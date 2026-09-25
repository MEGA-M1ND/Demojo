import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, fmtSeconds, fmtUsd, type Job, type Project, type Scene, type Storyboard, type StoryboardState } from '../api'
import { useAutosave, useJob } from '../hooks'
import { ErrorBox, JobProgress, Modal, Notice, SavedBadge } from './common'
import { SceneInspector, sceneThumb } from './SceneInspector'
import type { Step } from './Workspace'

function newSceneId() {
  const a = 'abcdefghijklmnopqrstuvwxyz0123456789'
  let s = 'scn_'
  for (let i = 0; i < 8; i++) s += a[Math.floor(Math.random() * a.length)]
  return s
}

export function ReviewStep({ project, reload, onNext, onBack }: { project: Project; reload: () => Promise<Project | null>; onNext: () => void; onBack: (s: Step) => void }) {
  const [state, setState] = useState<StoryboardState | null>(null)
  const [sb, setSb] = useState<Storyboard | null>(null)
  const [baseRev, setBaseRev] = useState<number | null>(null)
  const [selected, setSelected] = useState(0)
  const [error, setError] = useState<ApiError | null>(null)
  const [conflict, setConflict] = useState(false)
  const [storyJobId, setStoryJobId] = useState<string | null>(null)
  const [sceneJob, setSceneJob] = useState<{ id: string; sceneId: string } | null>(null)
  const [narrateJobId, setNarrateJobId] = useState<string | null>(null)
  const [proposal, setProposal] = useState<Job | null>(null)
  const [estimate, setEstimate] = useState<{ estimate_usd: number | null; note?: string; provider: string } | null>(null)
  const [lastChanges, setLastChanges] = useState<string[] | null>(null)
  const lastSaved = useRef<string>('')

  const load = useCallback(async () => {
    try {
      const st = await api.storyboard(project.id)
      setState(st)
      setSb(st.storyboard)
      setBaseRev(st.storyboard?.revision ?? null)
      lastSaved.current = st.storyboard ? JSON.stringify(st.storyboard.scenes) : ''
      setConflict(false)
      setError(null)
    } catch (e) {
      setError(e as ApiError)
    }
  }, [project.id])

  useEffect(() => {
    load()
    api.estimate(project.id).then(setEstimate).catch(() => setEstimate(null))
    api.jobs(project.id).then((jobs) => {
      const active = jobs.find((j) => j.active && j.kind === 'generate_story')
      if (active) setStoryJobId(active.id)
      const sj = jobs.find((j) => j.active && j.kind === 'regenerate_scene')
      if (sj) setSceneJob({ id: sj.id, sceneId: String(sj.params.scene_id) })
      const nj = jobs.find((j) => j.active && j.kind === 'narrate')
      if (nj) setNarrateJobId(nj.id)
    })
  }, [load, project.id])

  const save = useCallback(
    async (value: Storyboard | null) => {
      if (!value || baseRev === null) return
      const snap = JSON.stringify(value.scenes)
      if (snap === lastSaved.current) return
      try {
        const st = await api.saveStoryboard(project.id, value, baseRev)
        lastSaved.current = snap
        setBaseRev(st.storyboard!.revision)
        setState(st)
      } catch (e) {
        const err = e as ApiError
        if (err.body?.code === 'stale_revision') setConflict(true)
        throw e
      }
    },
    [project.id, baseRev],
  )
  const autosave = useAutosave(sb, save, 800, !conflict)

  const storyJob = useJob(storyJobId, async (j) => {
    if (j.status === 'story_ready') {
      setLastChanges((j.result?.changes as string[]) ?? [])
      await load()
      await reload()
    }
  })
  const sceneJobState = useJob(sceneJob?.id ?? null, (j) => {
    if (j.status === 'story_ready') setProposal(j)
  })
  const narrateJob = useJob(narrateJobId, () => load())

  const generate = async (regenerate: boolean) => {
    if (regenerate && !window.confirm(`Replace the storyboard with a new draft? Your current version stays available as revision ${baseRev} in the revision list.`)) return
    try {
      await autosave.flush()
      const { job } = await api.generateStory(project.id)
      setStoryJobId(job.id)
      setLastChanges(null)
    } catch (e) {
      setError(e as ApiError)
    }
  }

  const updateScene = (i: number, s: Scene) => setSb((cur) => (cur ? { ...cur, scenes: cur.scenes.map((x, j) => (j === i ? { ...s, origin: 'user' } : x)) } : cur))

  const moveScene = (i: number, d: number) =>
    setSb((cur) => {
      if (!cur) return cur
      const j = i + d
      if (j < 0 || j >= cur.scenes.length) return cur
      const scenes = [...cur.scenes]
      ;[scenes[i], scenes[j]] = [scenes[j], scenes[i]]
      scenes[0] = { ...scenes[0], transition_in: 'cut' }
      setSelected(j)
      return { ...cur, scenes }
    })

  const deleteScene = (i: number) => {
    if (!sb || sb.scenes.length <= 1) return
    if (!window.confirm(`Delete scene ${i + 1}?`)) return
    setSb({ ...sb, scenes: sb.scenes.filter((_, j) => j !== i).map((s, j) => (j === 0 ? { ...s, transition_in: 'cut' } : s)) })
    setSelected(Math.max(0, i - 1))
  }

  const addScene = (value: string) => {
    if (!sb || sb.scenes.length >= 8) return
    const base: Scene = {
      id: newSceneId(), role: 'feature', source_kind: 'title_card', asset_id: null, clip_in_s: null, clip_out_s: null, planned_duration_s: 3,
      headline: '', subline: '', narration: '', selection_reason: 'Added by you.', claims: [], fit_mode: 'contain', focus_region: null,
      focal_point: { x: 0.5, y: 0.5 }, highlight: null, motion: 'gentle_push_in', transition_in: 'dissolve', origin: 'user',
    }
    const a = project.assets.find((x) => x.id === value)
    if (a?.kind === 'video') Object.assign(base, { source_kind: 'clip', asset_id: a.id, clip_in_s: 0, clip_out_s: Math.min(a.duration_s ?? 5, 5), role: 'step', motion: 'static' })
    else if (a) Object.assign(base, { source_kind: 'image', asset_id: a.id, planned_duration_s: 4 })
    const scenes = [...sb.scenes]
    const at = Math.min(selected + 1, scenes.length)
    scenes.splice(at, 0, base)
    setSb({ ...sb, scenes })
    setSelected(at)
  }

  const regenerateScene = async (instruction: string) => {
    if (!sb || baseRev === null) return
    try {
      await autosave.flush()
      const { job } = await api.regenerateScene(project.id, sb.scenes[selected].id, baseRev, instruction)
      setSceneJob({ id: job.id, sceneId: sb.scenes[selected].id })
    } catch (e) {
      setError(e as ApiError)
    }
  }

  const acceptProposal = async (force = false) => {
    if (!proposal || baseRev === null) return
    try {
      await autosave.flush()
      const st = await api.acceptScene(project.id, String(proposal.result!.scene_id), proposal.id, baseRev, force)
      setState(st)
      setSb(st.storyboard)
      setBaseRev(st.storyboard!.revision)
      lastSaved.current = JSON.stringify(st.storyboard!.scenes)
      setProposal(null)
      setSceneJob(null)
    } catch (e) {
      const err = e as ApiError
      if (err.body?.detail && (err.body.detail as any).requires_force) {
        if (window.confirm(`${err.body.message}\n\nReplace your edits with the proposal?`)) return acceptProposal(true)
        return
      }
      setError(err)
    }
  }

  const revert = async (rev: number) => {
    if (baseRev === null || !window.confirm(`Restore revision ${rev}? The current version stays in the revision list.`)) return
    try {
      await autosave.flush()
      const st = await api.revertStoryboard(project.id, rev, baseRev)
      setState(st)
      setSb(st.storyboard)
      setBaseRev(st.storyboard!.revision)
      lastSaved.current = JSON.stringify(st.storyboard!.scenes)
    } catch (e) {
      setError(e as ApiError)
    }
  }

  const tl = state?.timeline
  const blockers = state?.blockers ?? []
  const issues = (state?.issues ?? []).filter((i) => i.level === 'error')
  const activeStory = storyJob.job?.active || (storyJobId && !storyJob.job)

  if (!state) return <div className="muted pad">Loading storyboard…</div>

  if (!sb) {
    return (
      <div className="step-body">
        <div className="step-intro">
          <h2>Review story</h2>
          <p className="muted">Generate a storyboard from your assets and description. You can edit every scene afterwards.</p>
        </div>
        {!project.readiness.ready && (
          <div className="readiness">
            <strong>Not ready yet:</strong>
            <ul>
              {project.readiness.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
            <button className="btn btn-ghost btn-sm" onClick={() => onBack(project.assets.length ? 'describe' : 'upload')}>
              Fix this →
            </button>
          </div>
        )}
        {storyJob.job ? <JobProgress job={storyJob.job} onCancel={() => api.cancelJob(storyJob.job!.id)} /> : null}
        <ErrorBox error={storyJob.job && !storyJob.job.active ? storyJob.job.error : null}>
          {storyJob.job?.error && <RetryButtons job={storyJob.job} onJob={(j) => setStoryJobId(j.id)} onError={setError} />}
        </ErrorBox>
        <ErrorBox error={error?.body} onDismiss={() => setError(null)} />
        <div className="generate-card card">
          <button className="btn btn-primary btn-lg" disabled={!project.readiness.ready || !!activeStory} onClick={() => generate(false)}>
            {activeStory ? 'Generating…' : 'Generate story'}
          </button>
          <div className="muted small">
            {estimate?.provider === 'openrouter'
              ? estimate.estimate_usd !== null
                ? `Estimated AI cost: up to ${fmtUsd(estimate.estimate_usd)} (conservative, from OpenRouter catalog prices). Project budget ${fmtUsd(project.ai_budget_usd)}.`
                : 'The cost of this step could not be estimated; Demojo will stop before any unpriced call.'
              : 'Fixture mode: no AI calls or charges.'}
          </div>
        </div>
      </div>
    )
  }

  const scene = sb.scenes[Math.min(selected, sb.scenes.length - 1)]
  const timingFor = (id: string) => tl?.scenes.find((t) => t.scene_id === id)

  return (
    <div className="step-body review">
      <div className="review-top">
        <div className="runtime">
          <div className="rt-numbers">
            <span>
              Requested <strong>{sb.output.target_duration_s}s</strong>
            </span>
            <span>
              Computed <strong className={tl?.over_target ? 'danger' : ''}>{fmtSeconds(tl?.computed_s)}</strong>
              <span className="muted small"> {tl?.all_speech_measured ? '(measured narration)' : sb.narration.mode === 'tts' ? '(estimated until narration is synthesised)' : ''}</span>
            </span>
          </div>
          {tl && (
            <div className="rt-bar" title="Scene lengths on the resolved timeline">
              {tl.scenes.map((t, i) => (
                <button
                  key={t.scene_id}
                  className={`rt-seg ${i === selected ? 'on' : ''}`}
                  style={{ flexGrow: t.n_frames - t.overlap_frames }}
                  onClick={() => setSelected(i)}
                  title={`Scene ${i + 1}: ${(t.n_frames / 30).toFixed(1)}s`}
                >
                  {i + 1}
                </button>
              ))}
            </div>
          )}
          {tl?.over_target && (
            <div className="small danger">
              Over the {sb.output.target_duration_s}s target by {(tl.computed_s - sb.output.target_duration_s).toFixed(1)}s. Shorten narration, or accept the adjusted runtime at export.
            </div>
          )}
        </div>
        <div className="review-actions">
          <SavedBadge state={autosave.state} error={autosave.error} />
          {sb.narration.mode === 'tts' && !tl?.all_speech_measured && (
            <button
              className="btn btn-ghost btn-sm"
              disabled={!!narrateJob.job?.active}
              onClick={async () => {
                try {
                  await autosave.flush()
                  const { job } = await api.narrate(project.id)
                  setNarrateJobId(job.id)
                } catch (e) {
                  setError(e as ApiError)
                }
              }}
              title="Synthesises narration (cached) so runtime is measured instead of estimated"
            >
              {narrateJob.job?.active ? 'Measuring…' : 'Measure narration'}
            </button>
          )}
          <button className="btn btn-ghost btn-sm" onClick={() => generate(true)} disabled={!!activeStory}>
            Regenerate story
          </button>
          {state.revisions && state.revisions.length > 1 && (
            <select aria-label="Revisions" value="" onChange={(e) => e.target.value && revert(+e.target.value)}>
              <option value="">Revisions…</option>
              {state.revisions
                .filter((r) => r.revision !== sb.revision)
                .map((r) => (
                  <option key={r.revision} value={r.revision}>
                    r{r.revision} · {r.source} · {r.note ?? ''}
                  </option>
                ))}
            </select>
          )}
        </div>
      </div>

      {conflict && (
        <div className="error-box">
          <strong>The storyboard changed elsewhere.</strong> Your latest edit was not saved.{' '}
          <button className="btn btn-sm btn-primary" onClick={load}>
            Reload latest
          </button>
        </div>
      )}
      {storyJob.job?.active && <JobProgress job={storyJob.job} onCancel={() => api.cancelJob(storyJob.job!.id)} />}
      {narrateJob.job?.active && <JobProgress job={narrateJob.job} onCancel={() => api.cancelJob(narrateJob.job!.id)} />}
      <ErrorBox error={narrateJob.job && !narrateJob.job.active ? narrateJob.job.error : null} />
      <ErrorBox error={storyJob.job && !storyJob.job.active ? storyJob.job.error : null} />
      <ErrorBox error={sceneJobState.job && !sceneJobState.job.active && sceneJobState.job.status !== 'story_ready' ? sceneJobState.job.error : null} />
      <ErrorBox error={autosave.state === 'error' && !conflict ? autosave.error?.body : null} />
      <ErrorBox error={error?.body} onDismiss={() => setError(null)} />

      {sb.generated_by === 'fixture' && <Notice tone="fixture">This storyboard is a fixture template built from your text — not AI-generated.</Notice>}
      {lastChanges && lastChanges.length > 0 && (
        <Notice>
          <strong>New draft generated.</strong> Changes from the previous revision: {lastChanges.slice(0, 6).join('; ')}
          {lastChanges.length > 6 ? '…' : ''}
        </Notice>
      )}
      {sb.warnings.length > 0 && (
        <details className="warnings" open>
          <summary>Notes about this story ({sb.warnings.length})</summary>
          <ul>
            {sb.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}
      {(blockers.length > 0 || issues.length > 0) && (
        <div className="blockers">
          <strong>Needs your attention before export</strong>
          <ul>
            {[...blockers, ...issues].map((b, i) => (
              <li key={i}>
                <button className="link" onClick={() => b.scene_id && setSelected(sb.scenes.findIndex((s) => s.id === b.scene_id))}>
                  {b.message}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {state.changes_since_export && state.changes_since_export.changes.length > 0 && (
        <details className="changes">
          <summary>Changes since the last export (revision {state.changes_since_export.revision}): {state.changes_since_export.changes.length}</summary>
          <ul>
            {state.changes_since_export.changes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </details>
      )}
      {state.changes_since_ai_draft && state.changes_since_ai_draft.changes.length > 0 && (
        <details className="changes">
          <summary>Your changes to the AI draft (revision {state.changes_since_ai_draft.revision}): {state.changes_since_ai_draft.changes.length}</summary>
          <ul>
            {state.changes_since_ai_draft.changes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </details>
      )}

      <div className="review-grid">
        <ol className="scene-list" aria-label="Scenes">
          {sb.scenes.map((s, i) => {
            const thumb = sceneThumb(project, s)
            const t = timingFor(s.id)
            const pending = s.claims.filter((c) => c.status === 'needs_confirmation').length
            return (
              <li key={s.id} className={`scene-card ${i === selected ? 'selected' : ''}`} data-testid="scene-card">
                <button className="scene-main" onClick={() => setSelected(i)}>
                  <div className="scene-thumb" style={!thumb ? { background: sb.branding.accent_color } : undefined}>
                    {thumb ? <img src={thumb} alt="" /> : <span className="tc-label">Title card</span>}
                    <span className="scene-num">{i + 1}</span>
                  </div>
                  <div className="scene-text">
                    <div className="scene-meta small">
                      <span className="pill">{s.role}</span>
                      <span className="muted">{s.source_kind === 'clip' ? `clip ${s.clip_in_s?.toFixed(1)}–${s.clip_out_s?.toFixed(1)}s` : s.source_kind === 'title_card' ? 'title card' : 'still'}</span>
                      <span className="muted">{t ? fmtSeconds(t.n_frames / 30) : ''}</span>
                      {s.origin === 'user' && <span className="pill pill-edited">edited</span>}
                      {pending > 0 && <span className="pill pill-warn">{pending} to confirm</span>}
                    </div>
                    <strong className="scene-headline">{s.headline || <span className="muted">(no headline)</span>}</strong>
                    <span className="scene-narr muted small">{s.narration || '(no narration)'}</span>
                    {s.selection_reason && <span className="scene-reason small">{s.selection_reason}</span>}
                  </div>
                </button>
                <div className="scene-tools">
                  <button className="icon-btn" aria-label="Move up" onClick={() => moveScene(i, -1)} disabled={i === 0}>
                    ↑
                  </button>
                  <button className="icon-btn" aria-label="Move down" onClick={() => moveScene(i, 1)} disabled={i === sb.scenes.length - 1}>
                    ↓
                  </button>
                  <button className="icon-btn danger" aria-label="Delete scene" onClick={() => deleteScene(i)} disabled={sb.scenes.length <= 1}>
                    ✕
                  </button>
                </div>
              </li>
            )
          })}
          {sb.scenes.length < 8 && (
            <li className="add-scene">
              <select aria-label="Add scene" value="" onChange={(e) => e.target.value && addScene(e.target.value)}>
                <option value="">+ Add scene after selected…</option>
                <option value="__title">Title card</option>
                {project.assets.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.kind === 'video' ? 'Recording' : 'Image'}: {a.label}
                  </option>
                ))}
              </select>
            </li>
          )}
        </ol>
        <SceneInspector
          key={scene.id}
          project={project}
          scene={scene}
          index={selected}
          timing={timingFor(scene.id)}
          narrationOn={sb.narration.mode === 'tts'}
          onChange={(s) => updateScene(selected, s)}
          onRegenerate={regenerateScene}
          regenBusy={!!sceneJobState.job?.active && sceneJob?.sceneId === scene.id}
        />
      </div>

      {sb.estimated_cost_usd !== null && (
        <p className="muted small">
          AI cost of this draft: {fmtUsd(sb.estimated_cost_usd)} {sb.cost_is_estimate ? '(includes estimates where OpenRouter reported no cost)' : '(reported by OpenRouter)'}.
        </p>
      )}

      <div className="step-actions">
        <button className="btn btn-ghost" onClick={() => onBack('describe')}>
          ← Describe
        </button>
        <button
          className="btn btn-primary"
          onClick={async () => {
            await autosave.flush()
            onNext()
          }}
        >
          Continue to Export →
        </button>
      </div>

      {proposal && proposal.result && (
        <Modal title={`Regenerated scene ${Number(proposal.result.scene_index) + 1}`} onClose={() => setProposal(null)} wide>
          <ProposalView current={sb.scenes.find((s) => s.id === proposal.result!.scene_id)} proposed={proposal.result.proposal as Scene} changes={proposal.result.changes as string[]} note={String(proposal.result.note ?? '')} />
          <div className="modal-actions">
            <button className="btn btn-ghost" onClick={() => setProposal(null)}>
              Discard
            </button>
            <button className="btn btn-primary" onClick={() => acceptProposal(false)}>
              Accept and replace scene
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}

function ProposalView({ current, proposed, changes, note }: { current?: Scene; proposed: Scene; changes: string[]; note: string }) {
  const row = (label: string, a?: string, b?: string) => (
    <tr className={a !== b ? 'diff' : ''}>
      <th>{label}</th>
      <td>{a || <span className="muted">—</span>}</td>
      <td>{b || <span className="muted">—</span>}</td>
    </tr>
  )
  return (
    <div>
      {note && <p className="muted small">{note}</p>}
      {changes.length > 0 && <p className="small">Changes: {changes.join('; ')}</p>}
      <table className="diff-table">
        <thead>
          <tr>
            <th />
            <th>Current</th>
            <th>Proposed</th>
          </tr>
        </thead>
        <tbody>
          {row('Headline', current?.headline, proposed.headline)}
          {row('Subline', current?.subline, proposed.subline)}
          {row('Narration', current?.narration, proposed.narration)}
          {row('Media', current ? `${current.source_kind}${current.clip_in_s !== null ? ` ${current.clip_in_s}–${current.clip_out_s}s` : ''}` : '', `${proposed.source_kind}${proposed.clip_in_s !== null ? ` ${proposed.clip_in_s}–${proposed.clip_out_s}s` : ''}`)}
          {row('Motion', current?.motion, proposed.motion)}
          {row('Claims', current?.claims.map((c) => c.text).join(' | '), proposed.claims.map((c) => `${c.text}${c.status === 'needs_confirmation' ? ' (needs confirmation)' : ''}`).join(' | '))}
        </tbody>
      </table>
    </div>
  )
}

export function RetryButtons({ job, onJob, onError }: { job: Job; onJob: (j: Job) => void; onError: (e: ApiError) => void }) {
  const retry = async () => {
    try {
      const { job: j } = await api.retryJob(job.id, false)
      onJob(j)
    } catch (e) {
      const err = e as ApiError
      if (err.body?.code === 'ambiguous_paid_request') {
        if (window.confirm(err.body.message)) {
          const { job: j } = await api.retryJob(job.id, true)
          onJob(j)
        }
        return
      }
      onError(err)
    }
  }
  if (!['failed', 'interrupted', 'canceled'].includes(job.status)) return null
  return (
    <button className="btn btn-sm btn-primary" onClick={retry}>
      Retry
    </button>
  )
}
