"""Job handlers executed by the worker."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import db, jobs
from . import ffmpeg as ff
from .analysis import _get_cached, analyze_assets, cache_key
from .config import get_settings
from .costs import Ledger, estimate_chat_usd, estimate_tts_usd
from .errors import AppError
from .narration import synthesize_all
from .planner import (
    build_context,
    plan_messages,
    planned_to_scene,
    scene_check,
    scene_messages,
    story_check,
    to_storyboard,
)
from .projects import (
    ProjectDetails,
    data_path,
    get_storyboard,
    new_id,
    project_dir,
    readiness,
    rel_path,
    save_storyboard,
    summarize_changes,
    validate_storyboard,
)
from .providers import get_provider
from .render import AssetMedia, RenderError, render
from .storyboard import Storyboard, export_blockers
from .timeline import resolve, to_script, to_srt


class JobContext:
    def __init__(self, job: dict, stop_event: threading.Event | None = None):
        self.job = job
        self.id = job["id"]
        self.pid = job["project_id"]
        self.params = db.loads(job["params_json"], {})
        self.stop_event = stop_event or threading.Event()
        self._last_check = 0.0
        self._canceled = False

    def stage(self, status: str | None, text: str, index: int | None = None, count: int | None = None) -> None:
        jobs.set_stage(self.id, status, text, index, count)

    def canceled(self) -> bool:
        if self._canceled or self.stop_event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_check > 0.5:
            self._last_check = now
            self._canceled = jobs.cancel_requested(self.id)
        return self._canceled

    def check(self) -> None:
        if self.canceled():
            raise AppError("canceled", "Canceled.")

    @property
    def tmp_dir(self) -> Path:
        return project_dir(self.pid) / "tmp" / self.id


def _details(pid: str) -> tuple[ProjectDetails, list[dict]]:
    with db.connect() as conn:
        row = conn.execute("SELECT details_json FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise AppError("canceled", "Project was deleted.")
        assets = [dict(r) for r in conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY position", (pid,))]
    return ProjectDetails.model_validate(db.loads(row["details_json"], {})), assets


def job_cost(job_id: str) -> tuple[float | None, bool]:
    with db.connect() as conn:
        rows = conn.execute("SELECT actual_usd, estimated_usd, status FROM provider_calls WHERE job_id = ?", (job_id,)).fetchall()
    if not rows:
        return None, False
    total = 0.0
    estimate = False
    for r in rows:
        if r["actual_usd"] is not None:
            total += r["actual_usd"]
        elif r["estimated_usd"] is not None and r["status"] != "failed":
            total += r["estimated_usd"]
            estimate = True
        elif r["status"] != "failed":
            estimate = True
    return round(total, 6), estimate


def preflight_story_estimate(provider, pid: str, assets: list[dict]) -> float | None:
    """Conservative estimate for analysis + planning (+ one repair each) before spending anything."""
    if provider.name != "openrouter":
        return 0.0
    client = provider.client
    vp = client.pricing(provider.vision_model, "chat")
    sp = client.pricing(provider.story_model, "chat")
    total = 0.0
    media = [a for a in assets if a["role"] == "media"]
    with db.connect() as conn:
        frames = {a["id"]: [r["t_s"] for r in conn.execute("SELECT t_s FROM asset_frames WHERE asset_id = ? ORDER BY idx", (a["id"],))]
                  for a in media if a["kind"] == "video"}
    for a in media:
        extra = a["classification"] + ("|" + ",".join(f"{t:.3f}" for t in frames.get(a["id"], [])))
        if _get_cached(cache_key(a["sha256"], provider.name, provider.vision_model, extra)) is not None:
            continue
        if a["kind"] == "video":
            est = estimate_chat_usd(vp, prompt_text_chars=3000, images=len(frames.get(a["id"], [])), max_completion_tokens=3000)
        else:
            est = estimate_chat_usd(vp, prompt_text_chars=1500, images=1, max_completion_tokens=1400)
        if est is None:
            return None
        total += est
    est = estimate_chat_usd(sp, prompt_text_chars=6000 + 1500 * len(media), images=0, max_completion_tokens=5000)
    if est is None:
        return None
    return total + est


def _check_budget(pid: str, estimate: float | None, what: str) -> None:
    with db.connect() as conn:
        prow = conn.execute("SELECT ai_budget_usd, allow_unpriced FROM projects WHERE id = ?", (pid,)).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_usd, estimated_usd, 0)), 0) FROM provider_calls"
            " WHERE project_id = ? AND status IN ('started','succeeded','ambiguous')", (pid,)).fetchone()[0]
    if estimate is None:
        if not prow["allow_unpriced"]:
            raise AppError("pricing_unavailable", f"Demojo could not price {what} reliably from OpenRouter's catalog and stopped before any call. "
                           "Allow unpriced calls for this project (and set an account-side credit limit) to continue.")
        return
    if spent + estimate > prow["ai_budget_usd"]:
        raise AppError("budget_exceeded",
                       f"{what.capitalize()} is estimated at up to ${estimate:.4f}; about ${spent:.4f} of this project's "
                       f"${prow['ai_budget_usd']:.2f} budget is used. Raise the budget to continue.",
                       detail={"estimate_usd": estimate, "spent_usd": spent, "budget_usd": prow["ai_budget_usd"]})


# ---------------------------------------------------------------------------
# generate_story
# ---------------------------------------------------------------------------


def run_generate_story(ctx: JobContext) -> None:
    details, assets = _details(ctx.pid)
    ready = readiness(details.model_dump(), assets)
    if not ready["ready"]:
        raise AppError("not_ready", " ".join(ready["problems"]))
    provider = get_provider()
    ledger = Ledger(ctx.pid, ctx.id)
    ctx.stage("analyzing", "Estimating AI cost")
    _check_budget(ctx.pid, preflight_story_estimate(provider, ctx.pid, assets), "story generation")
    ctx.check()
    analyses = analyze_assets(
        provider, ledger, ctx.pid, details.product_type,
        progress=lambda text, i, n: ctx.stage("analyzing", text, i, n), cancel_check=ctx.canceled,
    )
    ctx.check()
    ctx.stage("planning", "Drafting the storyboard")
    pctx = build_context(ctx.pid, details, analyses)
    planned = provider.plan_story(ledger, pctx, plan_messages(pctx), story_check(pctx))
    ctx.check()
    sb = to_storyboard(planned, pctx, provider)
    cost, is_est = job_cost(ctx.id)
    sb = Storyboard.model_validate(sb.model_dump() | {"estimated_cost_usd": cost, "cost_is_estimate": is_est})
    rep = validate_storyboard(ctx.pid, sb)
    if not rep.ok:
        raise AppError("invalid_storyboard", "; ".join(i.message for i in rep.errors))
    prev = get_storyboard(ctx.pid, required=False)
    note = "AI draft generated" if provider.name == "openrouter" else "Fixture template generated (not AI)"
    saved, _ = save_storyboard(ctx.pid, sb.model_dump(), base_revision=None, source=provider.name, note=note)
    jobs.finish_ok(ctx.id, "story_ready", {
        "revision": saved.revision,
        "previous_revision": prev.revision if prev else None,
        "changes": summarize_changes(prev, saved) if prev else [],
        "provider": provider.name,
        "cost_usd": cost,
        "cost_is_estimate": is_est,
    })


# ---------------------------------------------------------------------------
# regenerate_scene (produces a proposal; the user accepts it explicitly)
# ---------------------------------------------------------------------------


def run_regenerate_scene(ctx: JobContext) -> None:
    details, assets = _details(ctx.pid)
    base_rev = int(ctx.params["base_revision"])
    scene_id = ctx.params["scene_id"]
    sb = get_storyboard(ctx.pid, base_rev)
    idx = next((i for i, s in enumerate(sb.scenes) if s.id == scene_id), None)
    if idx is None:
        raise AppError("conflict", "That scene no longer exists in this storyboard revision.")
    provider = get_provider()
    ledger = Ledger(ctx.pid, ctx.id)
    if provider.name == "openrouter":
        sp = provider.client.pricing(provider.story_model, "chat")
        _check_budget(ctx.pid, estimate_chat_usd(sp, prompt_text_chars=12000, images=0, max_completion_tokens=1500), "scene regeneration")
    analyses = analyze_assets(provider, ledger, ctx.pid, details.product_type,
                              progress=lambda text, i, n: ctx.stage("planning", text, i, n), cancel_check=ctx.canceled)
    ctx.stage("planning", f"Rewriting scene {idx + 1}")
    pctx = build_context(ctx.pid, details, analyses)
    cur = sb.scenes[idx]
    rev_ref = {v: k for k, v in pctx.refs.items()}
    pctx.current_scene = {"asset_ref": rev_ref.get(cur.asset_id) if cur.asset_id else None, "source": cur.source_kind}
    out = provider.regenerate_scene(ledger, pctx, idx, scene_messages(pctx, sb, idx, ctx.params.get("instruction", "")),
                                    scene_check(pctx, idx))
    ctx.check()
    scene = planned_to_scene(out.scene, pctx, provider.name, scene_id=scene_id)
    if idx == 0:
        scene.transition_in = "cut"
    trial = sb.model_copy(deep=True)
    trial.scenes[idx] = scene
    rep = validate_storyboard(ctx.pid, trial)
    if not rep.ok:
        raise AppError("invalid_storyboard", "; ".join(i.message for i in rep.errors))
    changes = [c.replace(f"Scene {idx + 1}: ", "") for c in summarize_changes(sb, trial)]
    cost, is_est = job_cost(ctx.id)
    jobs.finish_ok(ctx.id, "story_ready", {
        "proposal": scene.model_dump(),
        "base_revision": base_rev,
        "scene_id": scene_id,
        "scene_index": idx,
        "note": out.note,
        "changes": changes,
        "provider": provider.name,
        "cost_usd": cost,
        "cost_is_estimate": is_est,
    })


# ---------------------------------------------------------------------------
# narrate / render
# ---------------------------------------------------------------------------


def _media(pid: str, sb: Storyboard) -> dict[str, AssetMedia]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM assets WHERE project_id = ?", (pid,))]
    out = {}
    for r in rows:
        path = data_path(r["master_path"] if r["kind"] in ("image", "logo") else r["original_path"])
        out[r["id"]] = AssetMedia(r["id"], r["kind"], path, r["sha256"], r["width"], r["height"], r["duration_s"], bool(r["has_alpha"]))
    return out


def _speech_preflight(provider, pid: str, sb: Storyboard) -> None:
    if provider.name != "openrouter" or sb.narration.mode != "tts":
        return
    from .narration import cached_speech  # noqa: PLC0415

    cached = cached_speech(pid, sb, provider)
    chars = sum(len(" ".join(s.narration.split())) for s in sb.scenes if s.narration.strip() and s.id not in cached)
    if chars == 0:
        return
    p = provider.client.pricing(provider.tts_model, "speech")
    _check_budget(pid, estimate_tts_usd(p, chars), "narration")


def _narrate(ctx: JobContext, sb: Storyboard):
    provider = get_provider()
    ledger = Ledger(ctx.pid, ctx.id)
    _speech_preflight(provider, ctx.pid, sb)
    try:
        return synthesize_all(provider, ledger, ctx.pid, sb,
                              progress=lambda text, i, n: ctx.stage("synthesizing", text, i, n), cancel_check=ctx.canceled)
    except AppError as e:
        if e.code in ("canceled",):
            raise
        # Never fall back to silence: surface the error with an explicit way forward.
        raise AppError(e.code, f"Narration failed: {e.message} You can retry, or re-export with “Continue without narration”.",
                       detail={"narration_failed": True, **(e.detail if isinstance(e.detail, dict) else {})},
                       ambiguous=e.ambiguous) from e


def run_narrate(ctx: JobContext) -> None:
    sb = get_storyboard(ctx.pid, ctx.job["revision"])
    speech, _files, new = _narrate(ctx, sb)
    tl = resolve(sb, speech)
    jobs.finish_ok(ctx.id, "completed", {"new_syntheses": new, "computed_s": round(tl.computed_s, 3),
                                          "requested_s": tl.requested_s, "over_target": tl.over_target})


def run_render(ctx: JobContext) -> None:
    quality = ctx.params.get("quality", "draft")
    if quality not in ("draft", "final"):
        raise AppError("conflict", "quality must be draft or final")
    sb = get_storyboard(ctx.pid, ctx.job["revision"])  # immutable snapshot
    if ctx.params.get("without_narration"):
        sb = Storyboard.model_validate(sb.model_dump() | {"narration": sb.narration.model_dump() | {"mode": "none"}})
    blockers = export_blockers(sb)
    if blockers:
        raise AppError("export_blocked", " ".join(b.message for b in blockers), detail=[b.as_dict() for b in blockers])
    rep = validate_storyboard(ctx.pid, sb)
    if not rep.ok:
        raise AppError("invalid_storyboard", "; ".join(i.message for i in rep.errors))
    speech, files, new = _narrate(ctx, sb)
    ctx.check()
    tl = resolve(sb, speech)
    if tl.over_target and not ctx.params.get("accept_runtime"):
        raise AppError(
            "runtime_over_target",
            f"With natural pacing the narration needs {tl.computed_s:.1f}s, but the target is {tl.requested_s:.0f}s. "
            "Shorten the narration, or accept the adjusted runtime.",
            detail={"computed_s": round(tl.computed_s, 2), "requested_s": tl.requested_s},
        )
    media = _media(ctx.pid, sb)
    work = ctx.tmp_dir
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    out_dir = work / "out"
    out_dir.mkdir()
    s = get_settings()

    def progress(text: str, frac: float | None) -> None:
        if text.startswith("Validating"):
            ctx.stage("validating", text)
        elif text.startswith("Rendering scene"):
            n = len(sb.scenes)
            i = int(round((frac or 0) * n))
            ctx.stage("rendering", text, i, n)
        else:
            ctx.stage("rendering", text)

    ctx.stage("rendering", "Preparing scenes", 0, len(sb.scenes))
    result = render(
        sb, tl, media, files, quality=quality, out_path=out_dir / "video.mp4", work_dir=work / "work",
        cache_dir=project_dir(ctx.pid) / "cache", threads=s.render_threads, timeout=s.ffmpeg_timeout_s,
        progress=progress, cancel_check=ctx.canceled,
    )
    ctx.check()
    (out_dir / "script.txt").write_text(to_script(sb, tl), encoding="utf-8")
    (out_dir / "captions.srt").write_text(to_srt(tl.captions), encoding="utf-8")
    (out_dir / "storyboard.json").write_text(sb.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "timeline.json").write_text(json.dumps(tl.as_dict(), indent=2), encoding="utf-8")
    shutil.rmtree(work / "work", ignore_errors=True)

    eid = new_id("exp")
    final_dir = project_dir(ctx.pid) / "exports" / eid
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    cost, is_est = job_cost(ctx.id)
    meta = {
        "timeline": {"computed_s": round(tl.computed_s, 3), "requested_s": tl.requested_s, "frames": tl.total_frames,
                     "over_target": tl.over_target, "accepted_runtime": bool(ctx.params.get("accept_runtime"))},
        "narration": {"mode": sb.narration.mode, "provider": get_provider().name, "new_syntheses": new,
                      "model": sb.narration.model, "voice": sb.narration.voice},
        "captions_note": "Captions use measured narration timing per scene; phrase timing within a scene is proportional (approximate), not word-aligned.",
        "generated_by": sb.generated_by,
        "scene_cache": {"hits": result.scene_cache_hits, "misses": result.scene_cache_misses},
        "cost_usd": cost,
        "cost_is_estimate": is_est,
        "warnings": result.warnings,
    }
    os.replace(out_dir, final_dir)
    with db.connect() as conn:
        with db.transaction(conn):
            published = jobs.finish_ok(ctx.id, "completed", {"export_id": eid, **meta}, conn=conn)
            if published:
                conn.execute(
                    "INSERT INTO exports(id, project_id, job_id, revision, quality, dir, width, height, duration_s, size_bytes,"
                    " has_audio, meta_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, ctx.pid, ctx.id, sb.revision, quality, rel_path(final_dir), result.width, result.height,
                     round(tl.computed_s, 3), result.size_bytes, 1 if result.has_audio else 0, db.dumps(meta), db.now_iso()),
                )
    if not published:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise AppError("canceled", "Canceled.")
    shutil.rmtree(work, ignore_errors=True)


HANDLERS = {
    "generate_story": run_generate_story,
    "regenerate_scene": run_regenerate_scene,
    "narrate": run_narrate,
    "render": run_render,
}


def execute(job: dict, stop_event: threading.Event | None = None) -> None:
    """Run one claimed job to a terminal state. Never raises."""
    ctx = JobContext(job, stop_event)
    try:
        HANDLERS[job["kind"]](ctx)
    except (ff.Canceled, AppError) as e:
        if isinstance(e, ff.Canceled) or e.code == "canceled":
            if ctx.stop_event.is_set():
                jobs.finish_error(ctx.id, "interrupted", AppError("interrupted", "The worker shut down while this job was running."))
            else:
                jobs.finish_error(ctx.id, "canceled", AppError("canceled", "Canceled by user."))
        else:
            jobs.finish_error(ctx.id, "failed", e)
    except RenderError as e:
        jobs.finish_error(ctx.id, "failed", AppError("render_failed", str(e)))
    except ff.FFmpegError as e:
        jobs.finish_error(ctx.id, "failed", AppError("render_failed", f"{e}", detail={"ffmpeg": e.stderr_tail[-600:]}))
    except Exception as e:  # pragma: no cover - defensive
        import traceback  # noqa: PLC0415

        jobs.finish_error(ctx.id, "failed", AppError("internal", f"{type(e).__name__}: {e}", detail={"trace": traceback.format_exc()[-1500:]}))
    finally:
        # Temporary files never outlive the job, whatever its outcome.
        shutil.rmtree(ctx.tmp_dir, ignore_errors=True)
