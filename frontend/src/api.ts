// Typed client for the Demojo API. No secrets are ever handled in the browser.

export type Rect = { x: number; y: number; w: number; h: number }
export type Point = { x: number; y: number }

export type Claim = {
  id: string
  text: string
  basis: 'user_detail' | 'visible_asset' | 'inferred'
  source_ref: string | null
  status: 'ok' | 'needs_confirmation' | 'confirmed' | 'removed'
}

export type Motion = 'static' | 'gentle_push_in' | 'pan_left' | 'pan_right' | 'focus_zoom'
export type SceneRole = 'hook' | 'intro' | 'feature' | 'step' | 'showcase' | 'cta'

export type Scene = {
  id: string
  role: SceneRole
  source_kind: 'image' | 'clip' | 'title_card'
  asset_id: string | null
  clip_in_s: number | null
  clip_out_s: number | null
  planned_duration_s: number
  headline: string
  subline: string
  narration: string
  selection_reason: string
  claims: Claim[]
  fit_mode: 'contain' | 'cover'
  focus_region: Rect | null
  focal_point: Point
  highlight: Rect | null
  motion: Motion
  transition_in: 'cut' | 'dissolve'
  origin: 'openrouter' | 'fixture' | 'user'
}

export type Storyboard = {
  schema_version: string
  project_id: string
  revision: number
  output: { aspect_ratio: '16:9' | '9:16'; fps: number; target_duration_s: 20 | 30 | 45 | 60 }
  branding: { product_name: string; accent_color: string; logo_asset_id: string | null; cta_text: string; website_text: string }
  style: 'clean_launch' | 'guided_walkthrough'
  product_type: 'software' | 'physical'
  narration: { mode: 'tts' | 'none'; model: string | null; voice: string | null }
  scenes: Scene[]
  warnings: string[]
  estimated_cost_usd: number | null
  cost_is_estimate: boolean
  generated_by: 'openrouter' | 'fixture' | 'user'
  generation_models: Record<string, string>
}

export type Issue = { level: 'error' | 'warning'; code: string; message: string; scene_id: string | null }

export type TimelineScene = {
  scene_id: string
  index: number
  start_frame: number
  n_frames: number
  transition_in: string
  overlap_frames: number
  clip_frames: number | null
  hold_frames: number
  speech: { start_frame: number; duration_s: number; end_frame: number; measured: boolean } | null
}

export type Timeline = {
  fps: number
  scenes: TimelineScene[]
  total_frames: number
  requested_s: number
  computed_s: number
  over_target: boolean
  tolerance_s: number
  all_speech_measured: boolean
  measured_scenes: string[]
}

export type StoryboardState = {
  storyboard: Storyboard | null
  issues?: Issue[]
  blockers?: Issue[]
  changes_since_export?: { revision: number; changes: string[] } | null
  changes_since_ai_draft?: { revision: number; changes: string[] } | null
  timeline?: Timeline
  revisions?: { revision: number; source: string; note: string | null; created_at: string }[]
}

export type Asset = {
  id: string
  role: 'media' | 'logo'
  kind: 'image' | 'video' | 'logo'
  classification: 'screenshot' | 'photo' | 'recording' | 'logo'
  label: string
  original_name: string
  position: number
  size_bytes: number
  mime: string
  width: number
  height: number
  duration_s: number | null
  fps: number | null
  has_audio: boolean
  has_alpha: boolean
  frames?: { index: number; t: number }[]
}

export type Details = {
  product_name: string
  description: string
  audience: string
  selling_points: string[]
  cta_text: string
  website_text: string
  product_type: 'software' | 'physical'
  style: 'clean_launch' | 'guided_walkthrough'
  target_duration_s: 20 | 30 | 45 | 60
  aspect_ratio: '16:9' | '9:16'
  narration: boolean
  script_notes: string
  workflow_notes: string
  accent_color: string
  logo_asset_id: string | null
}

export type Project = {
  id: string
  name: string
  details: Details
  current_revision: number | null
  ai_budget_usd: number
  allow_unpriced: boolean
  created_at: string
  updated_at: string
  assets: Asset[]
  logo: Asset | null
  readiness: { ready: boolean; problems: string[] }
}

export type ProjectSummary = {
  id: string
  name: string
  updated_at: string
  has_storyboard: boolean
  asset_count: number
  export_count: number
  thumb_asset_id: string | null
  details: Partial<Details>
}

export type ApiErrorBody = {
  code: string
  title: string
  message: string
  detail: unknown
  retryable: boolean
  ambiguous: boolean
}

export type Job = {
  id: string
  project_id: string
  kind: 'generate_story' | 'regenerate_scene' | 'narrate' | 'render'
  status: string
  active: boolean
  stage: string | null
  stage_index: number | null
  stage_count: number | null
  params: Record<string, unknown>
  revision: number | null
  result: Record<string, any> | null
  error: ApiErrorBody | null
  cancel_requested: boolean
  created_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_age_s: number | null
  retry_of: string | null
}

export type ExportItem = {
  id: string
  project_id: string
  job_id: string
  revision: number
  quality: 'draft' | 'final'
  width: number
  height: number
  duration_s: number
  size_bytes: number
  has_audio: boolean
  created_at: string
  meta: Record<string, any>
}

export type AppConfig = {
  provider_mode: 'openrouter' | 'fixture'
  fixture_mode: boolean
  models: { vision: string; story: string; tts: string; voice: string }
  limits: { max_images: number; max_image_mb: number; max_video_mb: number; max_video_seconds: number; max_upload_mb: number; max_image_mp: number }
  default_budget_usd: number
}

export type Costs = {
  budget_usd: number
  reported_usd: number
  estimated_unreported_usd: number
  unknown_cost_calls: number
  calls: number
  ambiguous_calls: number
  note: string
}

export class ApiError extends Error {
  status: number
  body: ApiErrorBody
  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.status = status
    this.body = body
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let data: any = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) {
    const err: ApiErrorBody = data?.error ?? {
      code: 'network',
      title: `Request failed (${res.status})`,
      message: text.slice(0, 200) || res.statusText,
      detail: null,
      retryable: true,
      ambiguous: false,
    }
    throw new ApiError(res.status, err)
  }
  return data as T
}

export const api = {
  config: () => request<AppConfig>('GET', '/api/config'),
  health: () => request<Record<string, unknown>>('GET', '/api/health'),
  listProjects: () => request<ProjectSummary[]>('GET', '/api/projects'),
  createProject: (name?: string) => request<Project>('POST', '/api/projects', { name }),
  getProject: (id: string) => request<Project>('GET', `/api/projects/${id}`),
  patchProject: (id: string, body: Partial<{ name: string; details: Partial<Details>; ai_budget_usd: number; allow_unpriced: boolean }>) =>
    request<Project>('PATCH', `/api/projects/${id}`, body),
  deleteProject: (id: string) => request<void>('DELETE', `/api/projects/${id}`),
  patchAsset: (pid: string, aid: string, body: { label?: string; classification?: 'screenshot' | 'photo' }) =>
    request<Asset>('PATCH', `/api/projects/${pid}/assets/${aid}`, body),
  deleteAsset: (pid: string, aid: string) => request<void>('DELETE', `/api/projects/${pid}/assets/${aid}`),
  reorderAssets: (pid: string, ids: string[]) => request<Asset[]>('POST', `/api/projects/${pid}/assets/reorder`, { ids }),
  storyboard: (pid: string) => request<StoryboardState>('GET', `/api/projects/${pid}/storyboard`),
  saveStoryboard: (pid: string, storyboard: Storyboard, base_revision: number) =>
    request<StoryboardState>('PATCH', `/api/projects/${pid}/storyboard`, { storyboard, base_revision }),
  revertStoryboard: (pid: string, revision: number, base_revision: number) =>
    request<StoryboardState>('POST', `/api/projects/${pid}/storyboard/revert`, { revision, base_revision }),
  generateStory: (pid: string) => request<{ job: Job; created: boolean }>('POST', `/api/projects/${pid}/storyboard/generate`),
  estimate: (pid: string) => request<{ provider: string; estimate_usd: number | null; budget_usd?: number; spent_usd?: number; note?: string; error?: ApiErrorBody }>(
    'GET', `/api/projects/${pid}/estimate`),
  regenerateScene: (pid: string, sceneId: string, base_revision: number, instruction: string) =>
    request<{ job: Job; created: boolean }>('POST', `/api/projects/${pid}/scenes/${sceneId}/regenerate`, { base_revision, instruction }),
  acceptScene: (pid: string, sceneId: string, job_id: string, base_revision: number, force = false) =>
    request<StoryboardState>('POST', `/api/projects/${pid}/scenes/${sceneId}/accept`, { job_id, base_revision, force }),
  narrate: (pid: string) => request<{ job: Job; created: boolean }>('POST', `/api/projects/${pid}/narration`),
  render: (pid: string, body: { quality: 'draft' | 'final'; accept_runtime?: boolean; without_narration?: boolean }) =>
    request<{ job: Job; created: boolean }>('POST', `/api/projects/${pid}/renders`, body),
  jobs: (pid: string) => request<Job[]>('GET', `/api/projects/${pid}/jobs`),
  job: (jid: string) => request<Job>('GET', `/api/jobs/${jid}`),
  cancelJob: (jid: string) => request<Job>('POST', `/api/jobs/${jid}/cancel`),
  retryJob: (jid: string, confirm_duplicate_charge = false) =>
    request<{ job: Job }>('POST', `/api/jobs/${jid}/retry`, { confirm_duplicate_charge }),
  exports: (pid: string) => request<ExportItem[]>('GET', `/api/projects/${pid}/exports`),
  costs: (pid: string) => request<Costs>('GET', `/api/projects/${pid}/costs`),
  feedback: (pid: string, body: { usefulness: number; biggest_issue: string; willingness_to_pay: string }) =>
    request<{ id: string }>('POST', `/api/projects/${pid}/feedback`, body),
}

export const urls = {
  assetThumb: (pid: string, aid: string) => `/api/projects/${pid}/assets/${aid}/content?variant=thumb`,
  assetOriginal: (pid: string, aid: string) => `/api/projects/${pid}/assets/${aid}/content?variant=original`,
  assetMaster: (pid: string, aid: string) => `/api/projects/${pid}/assets/${aid}/content?variant=master`,
  frame: (pid: string, aid: string, idx: number) => `/api/projects/${pid}/assets/${aid}/frames/${idx}`,
  exportFile: (pid: string, eid: string, file: string, download = false) =>
    `/api/projects/${pid}/exports/${eid}?file=${file}${download ? '&download=true' : ''}`,
}

/** Upload one file with real progress (XHR exposes upload progress; fetch does not). */
export function uploadAsset(
  pid: string,
  file: File,
  role: 'media' | 'logo',
  onProgress: (fraction: number) => void,
): { promise: Promise<Asset>; abort: () => void } {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<Asset>((resolve, reject) => {
    xhr.open('POST', `/api/projects/${pid}/assets?role=${role}&filename=${encodeURIComponent(file.name)}`)
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      let data: any = null
      try {
        data = JSON.parse(xhr.responseText)
      } catch {
        data = null
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as Asset)
      else
        reject(
          new ApiError(xhr.status, data?.error ?? { code: 'bad_upload', title: 'Upload failed', message: xhr.statusText || 'Upload failed', detail: null, retryable: true, ambiguous: false }),
        )
    }
    xhr.onerror = () => reject(new ApiError(0, { code: 'network', title: 'Network error', message: 'The upload was interrupted.', detail: null, retryable: true, ambiguous: false }))
    xhr.onabort = () => reject(new ApiError(0, { code: 'aborted', title: 'Upload canceled', message: 'Upload canceled.', detail: null, retryable: true, ambiguous: false }))
    xhr.send(file)
  })
  return { promise, abort: () => xhr.abort() }
}

export function fmtSeconds(s: number | null | undefined, digits = 1): string {
  if (s === null || s === undefined || Number.isNaN(s)) return '—'
  return `${s.toFixed(digits)}s`
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return 'unknown'
  if (n === 0) return '$0.00'
  if (n < 0.01) return `$${n.toFixed(4)}`
  return `$${n.toFixed(2)}`
}
