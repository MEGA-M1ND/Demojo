"""Cost estimation, budget reservation, and the provider-call ledger.

* Prices come from OpenRouter's documented model/endpoint metadata. We take the
  maximum price across a model's endpoints so estimates stay conservative.
* Every paid call is reserved (status 'started') before it is sent and settled
  afterwards with the provider-reported cost when available. Missing cost data
  is recorded as unknown/estimate, never as zero.
* The app-side budget is a guard, not a provider-enforced hard cap; use
  OpenRouter's per-key credit limits for an account-side cap.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from . import db
from .errors import AppError

PRICING_TTL_S = 6 * 3600
IMAGE_TOKENS_ESTIMATE = 1600  # conservative per-image token allowance for resized (<=1024px) inputs
CHARS_PER_TOKEN = 3.2  # conservative (over-counts tokens)


@dataclass
class Pricing:
    model: str
    unit: str  # "token" | "character"
    prompt: float | None
    completion: float | None
    image: float | None
    request: float | None
    reliable: bool
    note: str
    fetched_at: float

    def as_dict(self) -> dict:
        return asdict(self)


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def pricing_from_endpoints(model: str, kind: str, endpoints: list[dict]) -> Pricing:
    if not endpoints:
        return Pricing(model, "token", None, None, None, None, False, "no endpoints listed for this model", time.time())
    def mx(key: str) -> float | None:
        vals = [v for v in (_f((e.get("pricing") or {}).get(key)) for e in endpoints) if v is not None]
        return max(vals) if vals else None
    prompt, completion, image, request = mx("prompt"), mx("completion"), mx("image"), mx("request")
    if kind == "speech":
        reliable = prompt is not None and (completion or 0) == 0
        note = "per input character (max across endpoints)" if reliable else (
            "speech model also bills output/audio tokens; cannot estimate reliably from text length")
        return Pricing(model, "character", prompt, completion, None, request, reliable, note, time.time())
    reliable = prompt is not None and completion is not None
    return Pricing(model, "token", prompt, completion, image, request, reliable,
                   "per token, max across endpoints" if reliable else "missing prompt/completion price", time.time())


def cached_pricing(key: str) -> Pricing | None:
    with db.connect() as conn:
        row = conn.execute("SELECT json, fetched_at FROM pricing_cache WHERE key = ?", (key,)).fetchone()
    if row is None or time.time() - row["fetched_at"] > PRICING_TTL_S:
        return None
    return Pricing(**json.loads(row["json"]))


def store_pricing(key: str, p: Pricing) -> None:
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO pricing_cache(key, json, fetched_at) VALUES (?,?,?)",
                     (key, json.dumps(p.as_dict()), p.fetched_at))


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 16


def estimate_chat_usd(p: Pricing, *, prompt_text_chars: int, images: int, max_completion_tokens: int,
                      repair_allowance: bool = True) -> float | None:
    if not p.reliable or p.prompt is None or p.completion is None:
        return None
    prompt_tokens = int(prompt_text_chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS_ESTIMATE + 64
    cost = prompt_tokens * p.prompt + max_completion_tokens * p.completion
    cost += images * (p.image or 0.0) + (p.request or 0.0)
    if repair_allowance:
        # One bounded repair request: text-only, previous output + schema + errors.
        repair_prompt = int(prompt_text_chars / CHARS_PER_TOKEN) // 2 + max_completion_tokens + 800
        cost += repair_prompt * p.prompt + max_completion_tokens * p.completion + (p.request or 0.0)
    return cost


def estimate_tts_usd(p: Pricing, chars: int) -> float | None:
    if not p.reliable or p.prompt is None:
        return None
    return chars * p.prompt + (p.request or 0.0)


class Ledger:
    """Budget guard + record of paid provider calls for one project/job."""

    def __init__(self, project_id: str, job_id: str | None):
        self.project_id = project_id
        self.job_id = job_id

    def _spent(self, conn) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_usd, estimated_usd, 0)), 0) FROM provider_calls"
            " WHERE project_id = ? AND status IN ('started','succeeded','ambiguous')",
            (self.project_id,),
        ).fetchone()
        return float(row[0])

    def reserve(self, purpose: str, model: str, estimate_usd: float | None, *, chars: int | None = None) -> str:
        from .projects import new_id

        call_id = new_id("call")
        with db.connect() as conn:
            with db.transaction(conn):
                prow = conn.execute("SELECT ai_budget_usd, allow_unpriced FROM projects WHERE id = ?", (self.project_id,)).fetchone()
                if prow is None:
                    raise AppError("not_found", "Project not found.")
                budget = float(prow["ai_budget_usd"])
                spent = self._spent(conn)
                if estimate_usd is None and not prow["allow_unpriced"]:
                    raise AppError(
                        "pricing_unavailable",
                        f"The price of {model} could not be determined reliably from OpenRouter's catalog, so Demojo stopped "
                        "before calling it. Allow unpriced calls for this project (with an account-side OpenRouter credit limit), "
                        "or choose a model with listed pricing.",
                        detail={"model": model, "purpose": purpose},
                    )
                if estimate_usd is not None and spent + estimate_usd > budget:
                    raise AppError(
                        "budget_exceeded",
                        f"The next AI step ({purpose.replace('_', ' ')}) is estimated at up to ${estimate_usd:.4f}. "
                        f"This project has used about ${spent:.4f} of its ${budget:.2f} budget. Raise the project budget to continue.",
                        detail={"estimate_usd": estimate_usd, "spent_usd": spent, "budget_usd": budget, "purpose": purpose},
                    )
                conn.execute(
                    "INSERT INTO provider_calls(id, project_id, job_id, purpose, model, status, estimated_usd, cost_source,"
                    " chars, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (call_id, self.project_id, self.job_id, purpose, model, "started", estimate_usd,
                     "estimate" if estimate_usd is not None else "unknown", chars, db.now_iso()),
                )
        return call_id

    def settle(self, call_id: str, status: str, *, actual_usd: float | None = None, generation_id: str | None = None,
               error_code: str | None = None, prompt_tokens: int | None = None, completion_tokens: int | None = None) -> None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE provider_calls SET status = ?, actual_usd = COALESCE(?, actual_usd),"
                " cost_source = CASE WHEN ? IS NOT NULL THEN 'provider_reported' ELSE cost_source END,"
                " generation_id = COALESCE(?, generation_id), error_code = ?, prompt_tokens = ?, completion_tokens = ?,"
                " finished_at = ? WHERE id = ?",
                (status, actual_usd, actual_usd, generation_id, error_code, prompt_tokens, completion_tokens, db.now_iso(), call_id),
            )


def mark_ambiguous_calls() -> int:
    """On worker start: calls left 'started' by a dead process may or may not have been charged."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE provider_calls SET status = 'ambiguous', error_code = 'interrupted', finished_at = ? WHERE status = 'started'",
            (db.now_iso(),),
        )
        return cur.rowcount


def job_has_ambiguous_calls(job_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM provider_calls WHERE job_id = ? AND status = 'ambiguous' LIMIT 1", (job_id,)
        ).fetchone() is not None


def project_costs(pid: str) -> dict:
    with db.connect() as conn:
        prow = conn.execute("SELECT ai_budget_usd FROM projects WHERE id = ?", (pid,)).fetchone()
        rows = [dict(r) for r in conn.execute(
            "SELECT purpose, model, status, estimated_usd, actual_usd, cost_source, created_at FROM provider_calls"
            " WHERE project_id = ? ORDER BY created_at", (pid,))]
    reported = sum(r["actual_usd"] for r in rows if r["actual_usd"] is not None)
    est_unreported = sum(
        r["estimated_usd"] for r in rows
        if r["actual_usd"] is None and r["estimated_usd"] is not None and r["status"] in ("started", "succeeded", "ambiguous")
    )
    unknown = sum(1 for r in rows if r["actual_usd"] is None and r["estimated_usd"] is None and r["status"] != "failed")
    return {
        "budget_usd": prow["ai_budget_usd"] if prow else None,
        "reported_usd": round(reported, 6),
        "estimated_unreported_usd": round(est_unreported, 6),
        "unknown_cost_calls": unknown,
        "calls": len(rows),
        "ambiguous_calls": sum(1 for r in rows if r["status"] == "ambiguous"),
        "items": rows[-50:],
        "note": "Reported = cost returned by OpenRouter. Estimated = conservative app estimate where no cost was returned. "
                "The project budget is an app-side guard, not a provider-enforced cap.",
    }
