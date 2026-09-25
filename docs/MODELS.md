# Models, pricing assumptions, and live-integration status

Checked against OpenRouter's public model catalog and documentation on **25 September 2026**.
Re-run `demojo diagnose` to re-verify; it reads the catalog live and never prints the API key.

## Chosen defaults

| Role | Model ID | Why | Catalog facts used |
|---|---|---|---|
| Image and recording-frame analysis | `google/gemini-3.1-flash-lite` | Cheap, general-availability, accepts image input, supports strict structured outputs; no expiration date listed | `input_modalities` includes `image`; `supported_parameters` includes `structured_outputs`, `response_format`, `max_tokens`, `temperature` |
| Storyboard planning and scene regeneration | `google/gemini-3.1-flash-lite` | Same model: JSON-schema output, low cost | as above |
| Narration (TTS) | `hexgrad/kokoro-82m`, voice `af_heart` | Cheapest paid speech model in the catalog; English voices; priced per input character only | Listed under `GET /api/v1/models?output_modalities=speech`; `supported_voices` includes `af_heart` |

Alternatives I rejected:
- `google/gemini-2.5-flash-lite`: its catalog entry has `expiration_date` 2026-10-20.
- Gemini TTS models: they also bill output (audio) tokens, which Demojo can't estimate reliably from text length.

All model IDs and the voice can be changed in `.env` (`OPENROUTER_VISION_MODEL`, `OPENROUTER_STORY_MODEL`, `OPENROUTER_TTS_MODEL`, `OPENROUTER_TTS_VOICE`). Demojo never switches models silently. After changing one, run `demojo diagnose`: it checks image input, structured-output support, and that the voice is listed.

## Endpoints and parameters used

- `POST /api/v1/chat/completions`
  - Text first, then images as `image_url` base64 data URLs (resized ≤1024 px copies; recording frames ≤768 px, each preceded by a `Frame N at T s` label).
  - `response_format: {type: json_schema, json_schema: {strict: true, …}}`.
  - `provider: {require_parameters: true}`, so requests only go to endpoints that support structured output.
  - The cost comes from `usage.cost` in the response.
- `POST /api/v1/audio/speech` with `{model, input, voice, response_format: "mp3"}`.
  - The response is raw audio bytes. Demojo checks the content type, validates the audio with ffprobe, and measures its duration.
  - The cost is looked up with `GET /api/v1/generation?id=<X-Generation-Id>` when available; otherwise it's recorded as an estimate.
- `GET /api/v1/models`, `GET /api/v1/models/{id}/endpoints`: capabilities, voices, and per-endpoint pricing.
- `GET /api/v1/key`: diagnostics only.

## Pricing snapshot (max across endpoints, used for conservative estimates)

| Model | Prompt | Completion | Unit |
|---|---|---|---|
| google/gemini-3.1-flash-lite | $0.45 | $2.70 | per 1M tokens (list price $0.25 / $1.50; higher tiers exist) |
| hexgrad/kokoro-82m | $4.00 | — | per 1M input characters (endpoints range $0.62–$4.00) |

## Cost assumptions

- **Images:** estimated at 1,600 tokens each, plus any listed per-image price. **Text:** 1 token per 3.2 characters. Both over-count on purpose.
- **Output:** every chat estimate assumes the full `max_tokens` is used.
  - 1,400 tokens per image analysis, 3,000 per recording analysis, 5,000 for the plan, 1,500 per scene regeneration.
- **Repair:** the story-generation preflight includes one bounded repair request per call.
- **Narration:** characters × the per-character price.

With these assumptions, `demojo diagnose` gives a **typical-project upper bound of about $0.13**:

| Step | Upper bound |
|---|---|
| 6 image analyses | $0.058 |
| 24-frame recording analysis | $0.036 |
| Storyboard plan | $0.033 |
| 60 s of narration | $0.004 |

Actual charges should be lower. These are app estimates, not provider quotes.

## Budget behaviour

- **Before calling anything,** story generation and narration check a conservative estimate against the project budget (`AI_PROJECT_BUDGET_USD`, default $3).
- **Every paid call is also reserved** in the ledger before it is sent, and settled with the provider-reported cost.
- **If a model can't be priced reliably,** Demojo stops before the call and asks the user to allow unpriced calls for that project.
- **The budget is an app-side guard, not a provider-enforced cap.** Set a credit limit on the OpenRouter key for a hard account-side limit.
- **Missing cost data is shown as "estimate" or "unknown", never as $0.**

## Live integration status

**Verified against OpenRouter on 25 September 2026** with a rate-limited key supplied by the owner. The key lives only in the server's gitignored `.env`.

| Check | Result |
|---|---|
| `demojo diagnose --live` | Key accepted. One vision call with strict structured output returned valid JSON (`kind=software_screenshot`), and one `hexgrad/kokoro-82m` / `af_heart` / `mp3` call returned 2.09 s of decodable audio. Reported cost: $0.0010. |
| Live end-to-end: mixed 16:9 draft (3 screenshots + recording) | Analysis, planning, narration and render all succeeded. Reported cost: **$0.0134**. The story was grounded: every claim traced to the brief, with no numbers or testimonials added. It ran 38.0 s against the 30 s target, so the planner's word-budget check was tightened. |
| Live end-to-end: mixed 9:16 draft | Succeeded. Reported cost: **$0.0137**, runtime 30.5 s against 30 s. The planner set a portrait crop on the dashboard and a highlight on the KPI cards. |

Issues found live and fixed:
1. **Touching segments rejected.** Gemini returns recording segments that share a boundary frame, which the validator rejected even after the repair step. Touching segments are now accepted, and the mapped timestamps are trimmed so clips never overlap.
2. **Very thin crops.** The planner sometimes chose a crop strip about 7:1 wide, which floated in an empty frame. The renderer now widens over-thin crops symmetrically, within the image.
3. **Narration too long.** Narration could run up to 1.35× the word budget, so the planner check now allows at most 1.15×.

Later live runs, all passing:

| Run | Reported cost | Runtime vs 30 s target |
|---|---|---|
| Images-only 16:9 (physical product) | $0.0059 | 28.9 s |
| Recording-only 9:16 | $0.0089 | 23.5 s |
| Scene regeneration and accept | $0.0016 | — |
| Full UI walkthrough ([docs/demo/](demo/)) | $0.017 | 31.5 s |

More issues found live and fixed:

4. **Ungrounded outcome claims.** The AI wrote "Get paid faster", "free trial" and "instantly", none of which were in the brief. The planner prompt now forbids outcome and offer claims unless the user's text contains them, and the local claim guard flags them for confirmation.
5. **Crops slicing text.** AI crops cut through UI text at their edges. They now get a safety margin.

A real project costs about $0.013–0.017 per story plus narration, well under the conservative $0.13 upper-bound estimate.

Not yet done: listening to the audio by ear (it was only checked programmatically).
