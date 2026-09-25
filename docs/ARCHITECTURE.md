# Architecture

Demojo is a single-user local prototype with two processes that share one SQLite database and one data directory:

```
Browser (React + TS, served by FastAPI from frontend/dist)
   │  JSON + raw-body uploads + byte-range media        polling (1.2 s)
   ▼
API process — FastAPI (demojo.api)         Worker process — python -m demojo.worker
   project service, validation,               claims one job at a time (atomic UPDATE … RETURNING),
   enqueue jobs, serve media/exports          heartbeat every 5 s, cooperative cancel
          │                                         │
          └──────────── SQLite (WAL) + DATA_DIR ────┘
                         │
                 OpenRouter (vision, story, TTS) — only from the worker
                 FFmpeg / ffprobe — argument arrays, timeouts
```

## Modules (`backend/demojo/`)

| Module | Responsibility |
|---|---|
| `config.py` | Server-side settings from env / `.env` (the key never leaves the backend). |
| `db.py` | Schema: projects, assets, frames, analyses, storyboard revisions, jobs, exports, speech cache, provider-call ledger, feedback, pricing cache. |
| `projects.py` | Project service: details, content-sniffed uploads, ingest, reorder/rename/delete, storyboard revisions with optimistic concurrency, change summaries, exports, project deletion. |
| `media.py` | Image validation (formats, decoded-pixel limit, EXIF orientation), analysis copies, video probing (container/codec allowlist, rotation, duration, tail-decode check), timestamped frame sampling (uniform + scene changes, ≤24). |
| `storyboard.py` | **Canonical versioned storyboard** (`demojo.storyboard/1`) plus semantic validation against the project's real assets. |
| `ai_schemas.py` | Strict JSON Schemas sent as `response_format`, plus Pydantic models that re-validate every reply locally. |
| `providers/openrouter.py` | HTTP client, error classification, retry policy, one bounded repair, ledger integration. |
| `providers/openrouter_provider.py` | Provider methods: analyze image, analyze recording frames, plan storyboard, regenerate scene, synthesize speech. |
| `providers/fixture.py` | Explicit fixture mode only: deterministic templates from the user's own text, espeak-ng voice, always labelled "fixture". |
| `analysis.py` | Builds vision prompts; caches results by (content hash, prompt version, provider, model, frame times). |
| `planner.py` | Grounded planning prompts, semantic checks that feed the repair, conversion to scenes, and the local claim guard (unsupported numbers, superlatives, testimonial-like quotes → "needs confirmation"). |
| `narration.py` | TTS per scene, cached by (provider, model, voice, format, speed, text); audio validated and measured with ffprobe. |
| `timeline.py` | Integer-frame timeline resolved from measured speech. It is the single manifest for the video, the audio mix, the captions, and the reported runtime. |
| `text_render.py` | Pillow text layout with glyph-coverage checks and font fallback (Inter → DejaVu Sans, bundled). |
| `render.py` | Compositor and encoder: per-scene segments (cached), then the final join with cuts/dissolves, narration mix, and validation. |
| `costs.py` | Pricing from the catalog (max across endpoints), estimates, budget reservation and settlement, project cost summary. |
| `jobs.py`, `pipeline.py`, `worker.py` | Persistent job queue, job handlers, worker loop, interrupt recovery. |
| `cli.py` | `demojo diagnose | fixtures | smoke | serve | worker`. |

## Storyboard contract

Top level:
- `schema_version`, `project_id`, `revision`
- `output`: aspect ratio, 30 fps, target duration
- `branding`
- `style`, `product_type`
- `narration` mode
- ordered `scenes` (1–8)
- `warnings`, `estimated_cost_usd` + `cost_is_estimate`, `generated_by`

Each scene carries:
- a stable `id` and a `role`
- `source_kind` (`image | clip | title_card`), `asset_id`, and clip in/out
- `planned_duration_s`, `headline`, `subline`, `narration`, `selection_reason`
- `claims`, each with a basis (`user_detail | visible_asset | inferred`) and a status (`ok | needs_confirmation | confirmed | removed`)
- `fit_mode`, a normalised `focus_region` (crop), `focal_point`, and `highlight`
- `motion`, from an allowlist: `static, gentle_push_in, pan_left, pan_right, focus_zoom`
- `transition_in`: `cut | dissolve`
- `origin`

Providers return declarative data only, and it is untrusted:
- The model refers to assets by short refs (`A1`, `V1`) that are mapped locally. An unknown ref is a validation error.
- Recording segments are returned as frame indices and mapped back to source timestamps locally, so the model never invents times.
- Validation checks:
  - asset ownership, clip bounds, coordinates, scene count and runtime
  - renderable glyphs
  - chronological order of clips
  - confirmations required before export

Revisions are immutable rows:
- Every edit carries `base_revision`. A stale write gets `409 stale_revision` and is never applied.
- Scene regeneration produces a *proposal* against a base revision. Accepting it re-checks whether the user edited that scene in the meantime.
- Renders operate on a revision snapshot, so later edits create newer revisions without affecting a running render.

## Rendering

1. **Timeline.** Each scene gets `max(planned or clip length, lead-in + measured speech + tail, 1.5 s)` in integer frames.
   - Dissolves overlap by 12 frames.
   - A scene's speech offset is at least the overlap, so narration is never cut and never overlaps the previous scene's narration. There's a property test for this.
   - Clips shorter than their narration hold their final frame; they never loop.
2. **Scene segments.** Pillow composes frames and pipes raw RGB into `libx264`:
   - brand gradient background (with grain against banding)
   - the asset contained in a rounded card with a shadow
   - camera motion *inside the card*, using sub-pixel box resampling
   - an optional dimmed highlight with an accent outline
   - a headline, step chip, and logo overlay that the camera never moves
   - Recordings are decoded with FFmpeg at 30 fps into the same compositor, with rotation applied and timestamps zero-based.
   - Segments are cached by a hash of every pixel-affecting input, including the asset SHA-256, resolution, and frame counts.
3. **Final pass.** One FFmpeg filter graph joins the segments (xfade or concat) and places each narration clip at its exact timeline sample offset. Output is H.264 High / yuv420p / 30 fps, AAC 48 kHz, `+faststart`, BT.709 tags.
4. **Validation** before an export is published:
   - ffprobe: codec, dimensions, pixel format, 30/1 fps, duration within 2 frames of the timeline, and audio covering the last spoken frame
   - a full decode pass
5. **Publishing.** The output is moved into `exports/<id>/`. The export row is inserted in the same transaction that marks the job completed, and that transaction only succeeds if cancellation wasn't requested. Temporary files are always removed.

Draft (720p) and final (1080p) renders use the same code with different sizes and encoder settings.

## Jobs

- **Kinds:** `generate_story`, `regenerate_scene`, `narrate`, `render`.
- **Statuses:** `queued → analyzing | planning | synthesizing → rendering → validating → story_ready | completed`, plus `failed`, `canceled`, `interrupted`.
- **No duplicates:** a unique partial index on `dedupe_key` for active jobs stops repeated clicks from queueing twice.
- **Worker startup:** running jobs from a previous process are marked `interrupted`. Paid calls left `started` become `ambiguous`, and retrying such a job requires explicit confirmation. The UI explains that the request may already have been charged.
- **Retries:** only requests that provably never reached a provider are retried automatically (connection failures, 429/503 with `Retry-After` ≤ 15 s). Read timeouts are never retried.

## Storage layout (`DATA_DIR`)

```
demojo.sqlite3
projects/<prj_id>/assets/<ast_id>/original.*, master.*, thumb.jpg, analysis.jpg, frame_NN.jpg, poster.jpg
projects/<prj_id>/cache/tts/<hash>.mp3|wav
projects/<prj_id>/cache/scenes/<hash>.mp4
projects/<prj_id>/exports/<exp_id>/video.mp4, script.txt, captions.srt, storyboard.json, timeline.json
projects/<prj_id>/tmp/<job_id>/          (removed after every job)
tmp/uploads/                              (streamed uploads, removed after ingest)
```

Deleting a project:
- cancels its jobs and waits briefly for a running job to stop
- deletes its rows and its whole directory
- removes cached analyses whose content hash no other project uses

Feedback rows are kept, with the project reference cleared.

## Security notes (prototype scope)

- Clients only send IDs. Every lookup is scoped to the owning project, and paths are generated internally and resolved inside `DATA_DIR`.
- Upload type is detected from content, not the file name. Size limits are enforced while streaming. Images go through a decoded-pixel limit; video goes through a container/codec allowlist, a tail-decode check, and `-protocol_whitelist file,pipe`.
- User text is only drawn by Pillow. FFmpeg is only called with argument arrays, never through a shell, and its filter strings contain only numbers and generated labels. A test spies on every subprocess call during a render to confirm hostile headline text never appears in any argument.
- Text inside assets is treated as data in every prompt.
- There is no authentication. The Compose setup binds to `127.0.0.1`. Don't expose it publicly without a separate access-control task.

## Optional extension: "Animate product photo" (deferred)

Deferred so the core would ship first. The control is hidden (`/api/config` reports `generative_broll_enabled: false`), and `ENABLE_GENERATIVE_BROLL` is read but not acted on.

Suggested extension point:
- A new job kind `animate_photo` that takes one user-selected physical-product photo, with its own cost estimate and explicit opt-in.
- It would call OpenRouter's async video API, which needs its current schema, image transport, durations, and pricing re-verified. Local files must not be assumed to be remotely reachable.
- It would store the result as a new asset with `origin: generated` and an approve / reject / revert state.
- A scene could reference it only once it's approved; screenshots would be disallowed.
