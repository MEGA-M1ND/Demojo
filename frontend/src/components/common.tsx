import type { ReactNode } from 'react'
import type { ApiError, ApiErrorBody, Job } from '../api'
import type { SaveState } from '../hooks'

export function SavedBadge({ state, error }: { state: SaveState; error?: ApiError | null }) {
  const label =
    state === 'saving' ? 'Saving…' : state === 'dirty' ? 'Unsaved changes' : state === 'error' ? 'Not saved' : state === 'saved' ? 'All changes saved' : 'Saved'
  return (
    <span className={`saved-badge saved-${state}`} role="status" title={error?.body.message}>
      <span className="dot" aria-hidden />
      {label}
    </span>
  )
}

export function ErrorBox({ error, children, onDismiss }: { error: ApiErrorBody | null | undefined; children?: ReactNode; onDismiss?: () => void }) {
  if (!error) return null
  return (
    <div className="error-box" role="alert">
      <div className="error-head">
        <strong>{error.title}</strong>
        <code className="error-code">{error.code}</code>
        {onDismiss && (
          <button className="link" onClick={onDismiss} aria-label="Dismiss">
            Dismiss
          </button>
        )}
      </div>
      <p>{error.message}</p>
      {children && <div className="error-actions">{children}</div>}
    </div>
  )
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'fixture'; children: ReactNode }) {
  return <div className={`notice notice-${tone}`}>{children}</div>
}

const STATUS_LABEL: Record<string, string> = {
  queued: 'Queued',
  analyzing: 'Analysing assets',
  planning: 'Planning story',
  synthesizing: 'Synthesising narration',
  rendering: 'Rendering',
  validating: 'Validating output',
  story_ready: 'Story ready',
  completed: 'Completed',
  failed: 'Failed',
  canceled: 'Canceled',
  interrupted: 'Interrupted',
}

const STAGES: Record<string, string[]> = {
  generate_story: ['queued', 'analyzing', 'planning', 'story_ready'],
  regenerate_scene: ['queued', 'planning', 'story_ready'],
  narrate: ['queued', 'synthesizing', 'completed'],
  render: ['queued', 'synthesizing', 'rendering', 'validating', 'completed'],
}

/** Truthful stage-based progress: the list of stages, the current one, and the worker's own stage text. */
export function JobProgress({ job, onCancel }: { job: Job; onCancel?: () => void }) {
  const stages = STAGES[job.kind] ?? []
  const idx = stages.indexOf(job.status)
  const terminalBad = ['failed', 'canceled', 'interrupted'].includes(job.status)
  return (
    <div className={`job-progress ${terminalBad ? 'job-bad' : ''}`}>
      <ol className="stages">
        {stages.map((s, i) => {
          const state = terminalBad ? (i === 0 ? 'done' : 'todo') : i < idx ? 'done' : i === idx ? (job.active ? 'active' : 'done') : 'todo'
          return (
            <li key={s} className={`stage stage-${state}`}>
              <span className="stage-dot" aria-hidden />
              {STATUS_LABEL[s]}
            </li>
          )
        })}
        {terminalBad && <li className="stage stage-bad">{STATUS_LABEL[job.status]}</li>}
      </ol>
      <div className="job-line">
        <span className="job-stage">{job.stage || STATUS_LABEL[job.status]}</span>
        {job.stage_count ? (
          <span className="job-count">
            {Math.min(job.stage_index ?? 0, job.stage_count)} / {job.stage_count}
          </span>
        ) : null}
        {job.active && onCancel && (
          <button className="btn btn-ghost btn-sm" onClick={onCancel} disabled={job.cancel_requested}>
            {job.cancel_requested ? 'Canceling…' : 'Cancel'}
          </button>
        )}
      </div>
      {job.stage_count ? (
        <div className="bar" aria-hidden>
          <div className="bar-fill" style={{ width: `${Math.round(((job.stage_index ?? 0) / job.stage_count) * 100)}%` }} />
        </div>
      ) : job.active ? (
        <div className="bar bar-indeterminate" aria-hidden>
          <div className="bar-fill" />
        </div>
      ) : null}
    </div>
  )
}

export function Modal({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="link" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}
