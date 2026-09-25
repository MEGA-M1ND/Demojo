# Verification report

Build date: 25 September 2026. The environment was a Linux container with ffmpeg 6.1.1, Python 3.11, Node 22, and Playwright Chromium 141.

## Honest summary

| Area | Status |
|---|---|
| Renderer, timeline, captions, exports | **Implemented and tested.** Real MP4s were rendered and validated with ffprobe and a full decode pass. |
| Uploads, probing, persistence, worker jobs, cancel/interrupt/retry | **Implemented and tested** (pytest). |
| UI flow: upload → describe → review/edit → export → download | **Implemented and browser-tested** in fixture mode (Playwright). |
| OpenRouter vision, planning, scene regeneration, TTS, costs | **Verified live:** 5 end-to-end projects (images-only, recording-only, and mixed in both aspect ratios), scene regenerate and accept, and a full UI walkthrough, each $0.006–0.017. There are also mocked-transport tests for every error path. See [MODELS.md](MODELS.md#live-integration-status) and [demo/](demo/). |
| Docker image | **Built and smoke-tested in GitHub Actions**: the image renders a real video in fixture mode, and pushes to `main` publish it to `ghcr.io/mega-m1nd/demojo`. It couldn't be built inside the development sandbox because the egress policy blocks Debian mirrors. |
| Optional "Animate product photo" | **Deferred** (hidden; extension point documented). |

The committed `samples/` come from **fixture mode**: deterministic templates built from the fixture brief, and the local espeak-ng voice.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request:
- backend lint and the full pytest suite, including real renders
- frontend type-check, build and lint, plus the Playwright browser test
- a Docker image build and a smoke render inside that image

Pushes to `main` publish the image to `ghcr.io/<owner>/demojo`. CI never makes paid AI calls: it uses fixture mode and a mocked OpenRouter.

## Automated tests

Run `cd backend && uv run pytest -q`. **Result: 75 passed** (about 5 minutes; most of that is real renders).

| Requirement | Tests |
|---|---|
| Reject foreign asset IDs, out-of-bounds clip times, invalid focal/rect coordinates, unsupported schema output (unknown fields, wrong version, non-allowlisted motion/transition, empty or oversized scene lists) | `test_storyboard_validation.py` |
| Planner semantic checks on untrusted provider output (foreign refs, clip bounds, rects, clip chronology, title-card refs, overlong text); local claim guard for unsupported numbers and superlatives | `test_storyboard_validation.py` |
| Measured speech can't be truncated by scene timing (400 randomized storyboards); dissolve overlap accounting; final-frame hold instead of looping; runtime-over-target flag; SRT format | `test_timeline.py` |
| Images-only, recording-only, and mixed renders covering both aspect ratios. Each checks: H.264 / yuv420p, exact dimensions, 30/1 fps, duration within 1 frame of the timeline, AAC audio covering the last spoken frame, narration audible in the last speech window, moov-before-mdat (faststart), no black or flat frames at the opening, middle, and CTA, no `blackdetect` segments, captions present | `test_render.py` (4 cases, two of them at 1080p final) |
| Text with quotes, punctuation, Unicode, control characters, and filename- or shell-like attack strings renders safely; emoji and CJK are *reported* rather than drawn as boxes; a subprocess spy proves hostile headline text never reaches any process argument and `shell=True` is never used | `test_text_safety.py`, `test_render.py` |
| Provider failure is distinguishable from fixture or silent output: a TTS failure fails the job with its code and a "Continue without narration" option, publishes nothing, and only an explicit choice produces a silent (labelled) export; an analysis failure preserves the user's inputs | `test_pipeline.py` |
| Cached re-rendering doesn't repeat paid generation: a counting provider shows 0 new analysis or narration calls on re-render and resolution change, 1 new TTS call after editing one line, and scene-segment cache hits | `test_pipeline.py` |
| Canceled or interrupted jobs can't publish partial output: cancel during a 1080p render leaves no export row, no export files, and no tmp dir; `finish_ok` refuses after a cancel request; worker restart marks jobs interrupted and paid calls ambiguous; retry requires confirmation; stale heartbeats are reported | `test_jobs.py` |
| OpenRouter client (mocked transport): request shape (strict `json_schema`, `require_parameters`), reported cost recorded, exactly one repair, error classes (missing / invalid / expired key, insufficient credits, incompatible model, refused, unavailable), 429 `Retry-After` retried, **read timeout marked ambiguous and never retried**, budget and unpriced-model guards stop before sending, TTS audio validation | `test_openrouter.py` |
| Layout helpers: `contain` never stretches, the camera window stays inside the frame for every motion, headline bands never overlap the content area, logo-contrast backing rule, workflow-clause splitting | `test_render_units.py` |
| API: content-sniffed uploads; one-recording limit; corrupt, truncated, empty, fake-MP4, decompression-limit, and oversize uploads rejected with actionable errors; **HTTP 206 byte ranges**; cross-project asset access → 404; stale storyboard writes → 409; unconfirmed claims block export; duplicate render clicks deduped; revert; feedback | `test_api.py` |

UI screenshots from the browser test are in [`docs/screenshots/`](screenshots/).

### Browser test

Run `cd frontend && npm run build && npm run e2e`. **Result: 1 passed.** It starts an isolated backend in fixture mode, then:

1. Creates a project and uploads 3 screenshots and 1 recording through the real file input. It checks the progress rows, thumbnails, and recording duration, and reorders an asset.
2. Fills the Describe form and waits for autosave.
3. Generates the story, edits a scene headline with quotes and Unicode, waits for "All changes saved", and confirms the edit survives a page reload.
4. Renders the draft, watching the stage labels, until the player appears.
5. Checks **byte-range seeking** (HTTP 206, `video/mp4`), downloads the MP4 (checks the `ftyp` header and size) and the SRT.

**Playback limitation:** Playwright's open-source Chromium build reports no H.264/AAC support (`canPlayType('video/mp4; codecs="avc1…"') === ''`; VP9 only). The test asserts real playback only when the browser supports H.264, as Chrome, Edge, and Safari do. In this environment, in-browser playback was therefore **not** exercised. Decodability was verified with ffprobe and a full FFmpeg decode pass instead.

## Rendered-output inspection (manual)

I extracted and looked at frames at 0.6 s, 20 %, 40 %, 60 %, 80 %, and end−0.8 s of every sample, plus earlier proof renders.

- **Checked and OK:**
  - Headline legibility, and no clipping of UI text.
  - Screenshots stay sharp inside the card during push-in and zoom-to-highlight.
  - The highlight dims the rest of the card and outlines the region.
  - Headlines never cover the subject (the camera moves inside the card only).
  - Portrait groups headline and card, with the logo at the bottom.
  - Title and CTA cards (logo, CTA pill, website).
  - Recording segments show the actual recorded UI and cursor.
  - No black frames; dissolves blend correctly.
  - A hostile headline string renders literally.
- **Found and fixed during inspection:**
  1. Blocky artifacts in the background gradient (the gradient was rotated): replaced with a blended gradient plus light grain.
  2. Whole-frame focus zoom pushed the screenshot under the headline: the camera now moves inside the card.
  3. Portrait layout left a large gap: headline and card are now centred as a group.
  4. The product name repeated under a wordmark logo on title cards.
  5. A light logo was nearly invisible on the light theme: a contrast backing is added automatically.
  7. **Found in live AI output:** AI-chosen crops sliced through UI text at their edges. Crops now get a 3.5 % safety margin, and thin crops are widened.
  6. Fixture template mismatches: clip scenes used unrelated selling points, and still scenes had no narration. Clips now use the user's workflow steps in order, and selling point *i* goes with image *i*.
- **Audio:** I could not listen to the narration in this environment. Instead I checked it programmatically:
  - The AAC track exists and covers the last speech frame.
  - `volumedetect` over the last narration window is above −45 dB, so it isn't silent.
  - Narration clips are placed at the timeline's sample offsets.
  - The fixture voice is espeak-ng, so it will sound robotic by design. Kokoro (the default live voice) was not heard.
- **Pacing:** the fixture template budgets time per scene. The samples run 22–30 s against a 30 s target. Runs over target need explicit acceptance (the smoke test accepts automatically).

## Samples (`samples/`, fixture mode, 1080p final)

| File | Case | Aspect | Duration | Size |
|---|---|---|---|---|
| `physical_images_16x9_final.mp4` | Images only (Aurel, 3 photos) | 16:9 (1920×1080) | 28.5 s | 4.4 MB |
| `software_recording_9x16_final.mp4` | Recording only (Tallyfox, guided walkthrough) | 9:16 (1080×1920) | 22.0 s | 2.7 MB |
| `software_mixed_16x9_final.mp4` | Mixed: 3 screenshots + recording | 16:9 (1920×1080) | 29.6 s | 5.0 MB |
| `software_mixed_9x16_final.mp4` | Mixed: 3 screenshots + recording | 9:16 (1080×1920) | 29.6 s | 4.7 MB |

Each has a matching `.storyboard.json`, `.srt`, and `.script.txt`.

## Known limitations

- **Live OpenRouter behaviour has been checked on two fixture projects only.** Kokoro voice quality and its pronunciation of product names and URLs haven't been judged by ear.
- **English only.** The bundled fonts cover Latin, Greek, and Cyrillic; emoji and CJK are rejected with a message.
- **No narration speed control.** The TTS model lists no `speed` support.
- **Recordings:** the original audio is always muted, with no transcription. Analysis sees at most 24 sampled frames, so clip boundaries are suggestions for the user to correct.
- **Storyboard size:** at most 8 scenes and about 90 s of planned runtime.
- **Render speed:** CPU rendering takes roughly 45–65 s for a 30 s 1080p video on 4 cores here. Draft 720p takes about 15–20 s.
- **Deployment:** single user, no authentication, SQLite. Not for public deployment.

## Three most useful next improvements

1. **Tune on real customer projects.** Run the planning prompt on real screenshots and recordings, then listen to the narration and pick the best voice.
2. **Recording clean-up assistance.** Detect idle and pause stretches in recordings from frame differences (never adding fake motion) and suggest trims for the user to approve. Optionally transcribe the recording's own voice-over into the script notes, marked as a transcript.
3. **Faster, richer preview.** An in-browser scene preview (still frame plus camera path) so reviewers see framing changes without rendering, and parallel scene rendering to cut the full export time.
