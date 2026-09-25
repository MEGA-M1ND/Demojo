"""Application error taxonomy surfaced to the UI."""

from __future__ import annotations

# code -> (http status, short user-facing title)
ERROR_CODES: dict[str, tuple[int, str]] = {
    "missing_key": (400, "OpenRouter key missing"),
    "invalid_key": (401, "OpenRouter key rejected"),
    "insufficient_credits": (402, "Insufficient OpenRouter credits"),
    "incompatible_model": (400, "Model not compatible"),
    "rate_limited": (429, "Rate limited"),
    "provider_unavailable": (503, "AI provider unavailable"),
    "provider_timeout": (504, "AI request timed out"),
    "provider_invalid_response": (502, "AI returned an unusable response"),
    "provider_refused": (403, "AI provider refused the request"),
    "budget_exceeded": (402, "Project AI budget would be exceeded"),
    "pricing_unavailable": (409, "Model price unavailable"),
    "bad_upload": (400, "Upload rejected"),
    "unavailable_codec": (415, "Unsupported or corrupt media"),
    "invalid_storyboard": (422, "Storyboard is invalid"),
    "stale_revision": (409, "Storyboard changed elsewhere"),
    "export_blocked": (409, "Export needs your attention"),
    "runtime_over_target": (409, "Narration is longer than the target"),
    "narration_failed": (502, "Narration failed"),
    "render_failed": (500, "Render failed"),
    "not_ready": (409, "Project is not ready"),
    "conflict": (409, "Conflict"),
    "not_found": (404, "Not found"),
    "canceled": (409, "Canceled"),
    "interrupted": (409, "Interrupted"),
    "ambiguous_paid_request": (409, "A paid request may already have completed"),
    "fixture_voice_unavailable": (500, "Fixture voice unavailable"),
    "internal": (500, "Unexpected error"),
}


class AppError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None, detail: object | None = None,
                 retryable: bool = False, ambiguous: bool = False):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "internal"
        self.message = message
        self.status = status or ERROR_CODES[self.code][0]
        self.detail = detail
        self.retryable = retryable
        self.ambiguous = ambiguous

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "title": ERROR_CODES[self.code][1],
            "message": self.message,
            "detail": self.detail,
            "retryable": self.retryable,
            "ambiguous": self.ambiguous,
        }


def not_found(what: str = "Resource") -> AppError:
    return AppError("not_found", f"{what} not found.")
