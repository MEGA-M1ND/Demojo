import { useCallback, useEffect, useState } from 'react'
import { api, type ApiError, type AppConfig, type Project } from '../api'
import { ErrorBox, Notice } from './common'
import { DescribeStep } from './DescribeStep'
import { ExportStep } from './ExportStep'
import { ReviewStep } from './ReviewStep'
import { UploadStep } from './UploadStep'

export type Step = 'upload' | 'describe' | 'review' | 'export'

const STEPS: { id: Step; label: string }[] = [
  { id: 'upload', label: 'Upload' },
  { id: 'describe', label: 'Describe' },
  { id: 'review', label: 'Review story' },
  { id: 'export', label: 'Export' },
]

export function Workspace({ projectId, step, config, onStep }: { projectId: string; step: Step; config: AppConfig | null; onStep: (s: Step) => void }) {
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [name, setName] = useState('')

  const reload = useCallback(async () => {
    try {
      const p = await api.getProject(projectId)
      setProject(p)
      setName(p.name)
      setError(null)
      return p
    } catch (e) {
      setError(e as ApiError)
      return null
    }
  }, [projectId])

  useEffect(() => {
    reload()
  }, [reload])

  if (error && !project) return <ErrorBox error={error.body} />
  if (!project) return <div className="muted pad">Loading project…</div>

  const done: Record<Step, boolean> = {
    upload: project.assets.length > 0,
    describe: project.readiness.ready,
    review: project.current_revision !== null,
    export: false,
  }

  return (
    <div className="workspace">
      <div className="ws-head">
        <input
          className="title-input"
          aria-label="Project name"
          value={name}
          maxLength={80}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name.trim() && name !== project.name && api.patchProject(project.id, { name }).then(setProject).catch(setError)}
        />
        <nav className="stepper" aria-label="Steps">
          {STEPS.map((s, i) => (
            <button key={s.id} className={`step ${step === s.id ? 'step-current' : ''} ${done[s.id] ? 'step-done' : ''}`} onClick={() => onStep(s.id)}>
              <span className="step-num">{done[s.id] && step !== s.id ? '✓' : i + 1}</span>
              {s.label}
            </button>
          ))}
        </nav>
      </div>
      {config?.fixture_mode && (
        <Notice tone="fixture">
          <strong>Fixture mode.</strong> No AI calls are made. Stories are deterministic templates assembled from your own text, and narration uses a local
          robotic voice (espeak-ng). Do not present these outputs as AI-generated.
        </Notice>
      )}
      <ErrorBox error={error?.body} onDismiss={() => setError(null)} />
      {step === 'upload' && <UploadStep project={project} config={config} reload={reload} onNext={() => onStep('describe')} />}
      {step === 'describe' && <DescribeStep project={project} setProject={setProject} onNext={() => onStep('review')} />}
      {step === 'review' && <ReviewStep project={project} reload={reload} onNext={() => onStep('export')} onBack={onStep} />}
      {step === 'export' && <ExportStep project={project} onBack={onStep} />}
    </div>
  )
}
