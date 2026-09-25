import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, fmtBytes, fmtSeconds, fmtUsd, urls, type Costs, type ExportItem, type Job, type Project, type StoryboardState } from '../api'
import { useJob } from '../hooks'
import { ErrorBox, JobProgress, Notice } from './common'
import { RetryButtons } from './ReviewStep'
import type { Step } from './Workspace'

export function ExportStep({ project, onBack }: { project: Project; onBack: (s: Step) => void }) {
  const [state, setState] = useState<StoryboardState | null>(null)
  const [exportsList, setExports] = useState<ExportItem[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [costs, setCosts] = useState<Costs | null>(null)
  const [lastReq, setLastReq] = useState<{ quality: 'draft' | 'final'; accept_runtime: boolean; without_narration: boolean }>({ quality: 'draft', accept_runtime: false, without_narration: false })

  const load = useCallback(async () => {
    try {
      const [st, ex, c] = await Promise.all([api.storyboard(project.id), api.exports(project.id), api.costs(project.id)])
      setState(st)
      setExports(ex)
      setCosts(c)
      setSelected((cur) => cur ?? ex[0]?.id ?? null)
    } catch (e) {
      setError(e as ApiError)
    }
  }, [project.id])

  useEffect(() => {
    load()
    api.jobs(project.id).then((js) => {
      const active = js.find((j) => j.active && j.kind === 'render')
      if (active) setJobId(active.id)
    })
  }, [load, project.id])

  const { job } = useJob(jobId, async (j: Job) => {
    await load()
    if (j.status === 'completed' && j.result?.export_id) setSelected(String(j.result.export_id))
  })

  const start = async (req: { quality: 'draft' | 'final'; accept_runtime?: boolean; without_narration?: boolean }) => {
    const full = { accept_runtime: false, without_narration: false, ...req }
    setLastReq(full)
    setError(null)
    try {
      const { job: j } = await api.render(project.id, full)
      setJobId(j.id)
    } catch (e) {
      setError(e as ApiError)
    }
  }

  if (!state) return <div className="muted pad">Loading…</div>
  const sb = state.storyboard
  if (!sb) {
    return (
      <div className="step-body">
        <Notice>No storyboard yet. Generate and review the story first.</Notice>
        <button className="btn btn-primary" onClick={() => onBack('review')}>
          Go to Review
        </button>
      </div>
    )
  }
  const blockers = [...(state.blockers ?? []), ...(state.issues ?? []).filter((i) => i.level === 'error')]
  const tl = state.timeline
  const current = exportsList.find((e) => e.id === selected) ?? null
  const running = !!job?.active
  const err = job && !job.active ? job.error : null
  const runtimeDetail = err?.code === 'runtime_over_target' ? (err.detail as { computed_s: number; requested_s: number }) : null
  const narrationFailed = !!err && !!(err.detail as any)?.narration_failed

  return (
    <div className="step-body">
      <div className="step-intro">
        <h2>Export</h2>
        <p className="muted">
          Draft preview (720p) and final export (1080p, 30 fps) use the same timeline and renderer. Narration is synthesised from the approved script and cached, so
          re-rendering at another resolution does not repeat AI calls.
        </p>
      </div>

      {blockers.length > 0 && (
        <div className="blockers">
          <strong>Resolve these in Review before exporting:</strong>
          <ul>
            {blockers.map((b, i) => (
              <li key={i}>{b.message}</li>
            ))}
          </ul>
          <button className="btn btn-sm btn-primary" onClick={() => onBack('review')}>
            Back to Review
          </button>
        </div>
      )}

      <div className="export-controls card">
        <div className="rt-numbers">
          <span>
            Requested <strong>{sb.output.target_duration_s}s</strong>
          </span>
          <span>
            Computed <strong className={tl?.over_target ? 'danger' : ''}>{fmtSeconds(tl?.computed_s)}</strong>{' '}
            <span className="muted small">{tl?.all_speech_measured ? 'measured' : sb.narration.mode === 'tts' ? 'estimated; measured during render' : ''}</span>
          </span>
          <span className="muted small">
            {sb.output.aspect_ratio} · {sb.narration.mode === 'tts' ? `narrated (${sb.narration.voice ?? 'voice'})` : 'no narration'} · revision {sb.revision}
          </span>
        </div>
        <div className="btn-row">
          <button className="btn btn-ghost" disabled={running || blockers.length > 0} onClick={() => start({ quality: 'draft' })}>
            Render draft preview (720p)
          </button>
          <button className="btn btn-primary" disabled={running || blockers.length > 0} onClick={() => start({ quality: 'final' })}>
            Export final MP4 (1080p)
          </button>
        </div>
        {job && <JobProgress job={job} onCancel={() => api.cancelJob(job.id).then(() => undefined)} />}
        <ErrorBox error={err}>
          {runtimeDetail && (
            <>
              <button className="btn btn-sm btn-primary" onClick={() => start({ ...lastReq, accept_runtime: true })}>
                Accept {runtimeDetail.computed_s.toFixed(1)}s and render
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => onBack('review')}>
                Shorten the script
              </button>
            </>
          )}
          {narrationFailed && (
            <button className="btn btn-sm btn-ghost" onClick={() => start({ ...lastReq, without_narration: true })}>
              Continue without narration
            </button>
          )}
          {job && !runtimeDetail && <RetryButtons job={job} onJob={(j) => setJobId(j.id)} onError={setError} />}
        </ErrorBox>
        <ErrorBox error={error?.body} onDismiss={() => setError(null)} />
      </div>

      {current ? (
        <div className="export-view">
          <div className={`player ${current.height > current.width ? 'player-portrait' : ''}`}>
            <video key={current.id} style={{ aspectRatio: `${current.width} / ${current.height}` }} src={urls.exportFile(project.id, current.id, 'video')} controls preload="metadata" data-testid="export-video" />
          </div>
          <div className="export-side card">
            <h3>
              {current.quality === 'final' ? 'Final export' : 'Draft preview'} · {current.width}×{current.height}
            </h3>
            <div className="muted small">
              {fmtSeconds(current.duration_s)} · {fmtBytes(current.size_bytes)} · {current.has_audio ? 'narrated' : 'silent'} · revision {current.revision} ·{' '}
              {new Date(current.created_at).toLocaleString()}
            </div>
            {current.revision !== sb.revision && <Notice tone="warn">The storyboard changed after this export (now revision {sb.revision}). Render again to include the changes.</Notice>}
            {current.meta?.generated_by === 'fixture' && <Notice tone="fixture">Fixture output: template storyboard and local espeak-ng voice — not AI-generated.</Notice>}
            <div className="downloads">
              <a className="btn btn-primary" href={urls.exportFile(project.id, current.id, 'video', true)} download>
                Download MP4
              </a>
              <a className="btn btn-ghost" href={urls.exportFile(project.id, current.id, 'script', true)} download>
                Narration script (.txt)
              </a>
              <a className="btn btn-ghost" href={urls.exportFile(project.id, current.id, 'captions', true)} download>
                Captions (.srt)
              </a>
              <a className="btn btn-ghost" href={urls.exportFile(project.id, current.id, 'storyboard', true)} download>
                Storyboard (.json)
              </a>
            </div>
            <p className="muted small">
              Captions use the measured narration timing for each scene. Phrase timing inside a scene is proportional to text length, so it is approximate — not
              word-synchronised.
            </p>
            {current.meta?.timeline?.accepted_runtime && <p className="muted small">You accepted an adjusted runtime of {fmtSeconds(current.meta.timeline.computed_s)}.</p>}
            {typeof current.meta?.cost_usd === 'number' && (
              <p className="muted small">
                AI cost for this render (narration): {fmtUsd(current.meta.cost_usd)}
                {current.meta.cost_is_estimate ? ' (estimate)' : ''}
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="empty">
          <p>No exports yet.</p>
          <p className="muted small">Start with a draft preview; it is quicker and uses the same composition as the final export.</p>
        </div>
      )}

      {exportsList.length > 1 && (
        <div className="export-history">
          <h3>Previous exports</h3>
          <ul>
            {exportsList.map((e) => (
              <li key={e.id}>
                <button className={`link ${e.id === selected ? 'on' : ''}`} onClick={() => setSelected(e.id)}>
                  {e.quality} · {e.width}×{e.height} · {fmtSeconds(e.duration_s)} · r{e.revision} · {new Date(e.created_at).toLocaleString()}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="bottom-grid">
        <FeedbackForm projectId={project.id} />
        {costs && (
          <div className="card costs">
            <h3>AI usage for this project</h3>
            {costs.calls === 0 ? (
              <div className="small">No paid AI calls have been made for this project yet.</div>
            ) : (
              <>
                <div className="small">
                  Reported by OpenRouter: <strong>{fmtUsd(costs.reported_usd)}</strong>
                </div>
                <div className="small">
                  Estimated where no cost was reported: <strong>{fmtUsd(costs.estimated_unreported_usd)}</strong>
                </div>
              </>
            )}
            {costs.unknown_cost_calls > 0 && <div className="small danger">{costs.unknown_cost_calls} call(s) with unknown cost</div>}
            {costs.ambiguous_calls > 0 && <div className="small danger">{costs.ambiguous_calls} call(s) with an unknown outcome (interrupted)</div>}
            <div className="small">
              Budget guard: {fmtUsd(costs.budget_usd)} · {costs.calls} paid call(s)
            </div>
            <p className="muted small">{costs.note}</p>
          </div>
        )}
      </div>
    </div>
  )
}

function FeedbackForm({ projectId }: { projectId: string }) {
  const [usefulness, setUsefulness] = useState<number | null>(null)
  const [issue, setIssue] = useState('')
  const [pay, setPay] = useState('')
  const [sent, setSent] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  if (sent)
    return (
      <div className="card">
        <h3>Thanks for the feedback</h3>
        <p className="muted small">Saved locally on this machine.</p>
      </div>
    )
  return (
    <form
      className="card feedback"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!usefulness) return
        try {
          await api.feedback(projectId, { usefulness, biggest_issue: issue, willingness_to_pay: pay })
          setSent(true)
        } catch (err) {
          setError(err as ApiError)
        }
      }}
    >
      <h3>Quick feedback</h3>
      <div className="small">How useful was this video? (1–5)</div>
      <div className="seg seg-sm">
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} type="button" className={usefulness === n ? 'on' : ''} onClick={() => setUsefulness(n)}>
            {n}
          </button>
        ))}
      </div>
      <label className="small">
        Biggest issue
        <textarea rows={2} value={issue} maxLength={2000} onChange={(e) => setIssue(e.target.value)} />
      </label>
      <label className="small">
        Would you pay for this? How much?
        <input value={pay} maxLength={200} onChange={(e) => setPay(e.target.value)} placeholder="e.g. $20/month, $15 per video, no" />
      </label>
      <p className="muted small">This is feedback only — there is no payment flow.</p>
      <ErrorBox error={error?.body} />
      <button className="btn btn-primary btn-sm" type="submit" disabled={!usefulness}>
        Send feedback
      </button>
    </form>
  )
}
