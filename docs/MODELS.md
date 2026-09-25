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

**Not verified against OpenRouter in this build session.** The `OPENROUTER_API_KEY` in the build environment was rejected by OpenRouter with `401 API key expired`. `demojo diagnose` classifies this correctly as `invalid_key`.

Verified without a working key:
- The public catalog: model IDs, capabilities, voices, pricing.
- The request and response handling, error classification, repair loop, ambiguous-timeout handling, and budget and pricing guards. These are tested against a mocked HTTP transport in `backend/tests/test_openrouter.py`.

Still untested live:
1. A real image-analysis call with structured output. This includes how Gemini handles the strict schema's nullable object fields, which are validated locally and repaired if needed.
2. A real storyboard-planning call and a real scene-regeneration call.
3. A real `/audio/speech` call with `hexgrad/kokoro-82m` / `af_heart` / `mp3`, including whether `X-Generation-Id` cost lookup returns a value immediately.
4. Real `usage.cost` values and the resulting cost display.

To verify once a valid key is in `.env`, run `cd backend && uv run demojo diagnose --live`. It makes one small vision call and one short TTS call, costing a fraction of a cent, and reports the results.
