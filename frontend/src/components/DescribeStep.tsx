import { useState } from 'react'
import { api, type Details, type Project } from '../api'
import { useAutosave } from '../hooks'
import { ErrorBox, Field, SavedBadge } from './common'

export function DescribeStep({ project, setProject, onNext }: { project: Project; setProject: (p: Project) => void; onNext: () => void }) {
  const [d, setD] = useState<Details>(project.details)
  const { state, error, flush } = useAutosave(d, async (v) => {
    const p = await api.patchProject(project.id, { details: v })
    setProject(p)
  })
  const set = <K extends keyof Details>(k: K, v: Details[K]) => setD((x) => ({ ...x, [k]: v }))
  const hasVideo = project.assets.some((a) => a.kind === 'video')
  const needsWorkflow = d.product_type === 'software' && !hasVideo

  return (
    <div className="step-body">
      <div className="step-intro row-between">
        <div>
          <h2>Describe the product</h2>
          <p className="muted">Facts you enter here are what the story may state. Anything the AI cannot trace to your text or your assets is flagged for your confirmation.</p>
        </div>
        <SavedBadge state={state} error={error} />
      </div>
      <ErrorBox error={error?.body} />

      <div className="form-grid">
        <div className="card">
          <h3>Product</h3>
          <Field label="Product name" htmlFor="pn">
            <input id="pn" value={d.product_name} maxLength={60} onChange={(e) => set('product_name', e.target.value)} />
          </Field>
          <Field label="Brief description" htmlFor="desc" hint={`${d.description.length}/1200 — what it is and what it does, in plain sentences.`}>
            <textarea id="desc" rows={4} value={d.description} maxLength={1200} onChange={(e) => set('description', e.target.value)} />
          </Field>
          <Field label="Intended audience" htmlFor="aud">
            <input id="aud" value={d.audience} maxLength={200} onChange={(e) => set('audience', e.target.value)} placeholder="e.g. Freelancers and small studios" />
          </Field>
          <Field label="Selling points (optional, up to three)">
            {[0, 1, 2].map((i) => (
              <input
                key={i}
                aria-label={`Selling point ${i + 1}`}
                className="stack-input"
                value={d.selling_points[i] ?? ''}
                maxLength={140}
                placeholder={`Selling point ${i + 1}`}
                onChange={(e) => {
                  const sp = [...d.selling_points]
                  sp[i] = e.target.value
                  set('selling_points', sp)
                }}
              />
            ))}
          </Field>
          <div className="two-col">
            <Field label="Call to action" htmlFor="cta">
              <input id="cta" value={d.cta_text} maxLength={60} onChange={(e) => set('cta_text', e.target.value)} placeholder="e.g. Start free today" />
            </Field>
            <Field label="Website text (optional)" htmlFor="web" hint="Shown as text only; the URL is never fetched.">
              <input id="web" value={d.website_text} maxLength={80} onChange={(e) => set('website_text', e.target.value)} placeholder="example.com" />
            </Field>
          </div>
        </div>

        <div className="card">
          <h3>Video</h3>
          <Field label="Type">
            <div className="seg">
              {(['software', 'physical'] as const).map((t) => (
                <button key={t} className={d.product_type === t ? 'on' : ''} onClick={() => set('product_type', t)} type="button">
                  {t === 'software' ? 'Software demo' : 'Physical product showcase'}
                </button>
              ))}
            </div>
          </Field>
          <Field label="Style">
            <div className="seg">
              {(['clean_launch', 'guided_walkthrough'] as const).map((t) => (
                <button key={t} className={d.style === t ? 'on' : ''} onClick={() => set('style', t)} type="button">
                  {t === 'clean_launch' ? 'Clean product launch' : 'Guided walkthrough'}
                </button>
              ))}
            </div>
          </Field>
          <div className="two-col">
            <Field label="Target duration">
              <div className="seg">
                {([20, 30, 45, 60] as const).map((t) => (
                  <button key={t} className={d.target_duration_s === t ? 'on' : ''} onClick={() => set('target_duration_s', t)} type="button">
                    {t}s
                  </button>
                ))}
              </div>
            </Field>
            <Field label="Aspect ratio">
              <div className="seg">
                {(['16:9', '9:16'] as const).map((t) => (
                  <button key={t} className={d.aspect_ratio === t ? 'on' : ''} onClick={() => set('aspect_ratio', t)} type="button">
                    {t === '16:9' ? 'Landscape 16:9' : 'Portrait 9:16'}
                  </button>
                ))}
              </div>
            </Field>
          </div>
          <Field label="Narration" hint={d.narration ? 'An AI voice reads the approved script. Captions are exported either way.' : 'Silent video; narration text is kept for captions.'}>
            <label className="toggle">
              <input type="checkbox" checked={d.narration} onChange={(e) => set('narration', e.target.checked)} />
              <span>{d.narration ? 'On' : 'Off'}</span>
            </label>
          </Field>
          <Field
            label={needsWorkflow ? 'Describe the actual workflow (required)' : 'Workflow notes (optional)'}
            htmlFor="wf"
            hint={
              needsWorkflow
                ? 'Screenshots alone cannot show how the product is used. Describe the real steps, e.g. “Open the dashboard, click New invoice, add line items, click Send”.'
                : 'Steps shown in your recording, in order. Helps the story match what actually happens.'
            }
          >
            <textarea id="wf" rows={3} value={d.workflow_notes} maxLength={1500} onChange={(e) => set('workflow_notes', e.target.value)} className={needsWorkflow && d.workflow_notes.trim().length < 15 ? 'needs' : ''} />
          </Field>
          <Field label="Script or notes (optional)" htmlFor="notes" hint="Paste a draft script or important points (including anything spoken in your recording — its audio is not used).">
            <textarea id="notes" rows={4} value={d.script_notes} maxLength={3000} onChange={(e) => set('script_notes', e.target.value)} />
          </Field>
        </div>
      </div>

      {!project.readiness.ready && (
        <div className="readiness">
          <strong>Before generating a story:</strong>
          <ul>
            {project.readiness.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="step-actions">
        <span />
        <button
          className="btn btn-primary"
          onClick={async () => {
            await flush()
            onNext()
          }}
        >
          Continue to Review →
        </button>
      </div>
    </div>
  )
}
