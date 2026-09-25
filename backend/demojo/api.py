"""HTTP API (FastAPI). Single-user local prototype — bind to 127.0.0.1 only."""

from __future__ import annotations

import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import costs, db, jobs, projects
from .config import get_settings
from .errors import AppError
from .narration import cached_speech
from .providers import get_provider
from .storyboard import Scene, export_blockers
from .timeline import resolve


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Demojo", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={"error": exc.as_dict()})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()[:5])
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "title": "Invalid request",
                                                            "message": msg, "detail": None, "retryable": False, "ambiguous": False}})


# ---------------------------------------------------------------------------
# Health / config
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    ok_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    writable = os.access(s.data_dir, os.W_OK)
    with db.connect() as conn:
        queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
        running = conn.execute(
            f"SELECT MAX(heartbeat_at) FROM jobs WHERE status IN ({','.join('?' * len(db.RUNNING_STATUSES))})", db.RUNNING_STATUSES
        ).fetchone()[0]
    return {
        "ok": ok_ffmpeg and writable,
        "ffmpeg": ok_ffmpeg,
        "data_dir_writable": writable,
        "provider_mode": s.provider_mode,
        "openrouter_key_configured": bool(s.openrouter_api_key),
        "queued_jobs": queued,
        "running_job_heartbeat_age_s": round(db.now_ts() - running, 1) if running else None,
    }


@app.get("/api/config")
def config() -> dict:
    s = get_settings()
    p = get_provider()
    return {
        "provider_mode": s.provider_mode,
        "fixture_mode": s.fixture_mode,
        "models": {"vision": p.vision_model, "story": p.story_model, "tts": p.tts_model, "voice": p.tts_voice},
        "limits": {
            "max_images": s.max_images, "max_image_mb": s.max_image_mb, "max_video_mb": s.max_video_mb,
            "max_video_seconds": s.max_video_seconds, "max_upload_mb": s.max_upload_mb, "max_image_mp": s.max_image_pixels / 1e6,
        },
        "default_budget_usd": s.ai_project_budget_usd,
        "generative_broll_enabled": False,  # optional extension deferred; see docs/ARCHITECTURE.md
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class CreateProject(BaseModel):
    name: str | None = Field(default=None, max_length=80)


class PatchProject(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    details: dict | None = None
    ai_budget_usd: float | None = None
    allow_unpriced: bool | None = None


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return projects.list_projects()


@app.post("/api/projects", status_code=201)
def create_project(body: CreateProject) -> dict:
    return projects.create_project(body.name)


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    return projects.get_project(pid)


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: PatchProject) -> dict:
    return projects.update_project(pid, name=body.name, details=body.details, ai_budget_usd=body.ai_budget_usd,
                                   allow_unpriced=body.allow_unpriced)


@app.delete("/api/projects/{pid}", status_code=204)
def delete_project(pid: str) -> None:
    projects.delete_project(pid)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@app.post("/api/projects/{pid}/assets", status_code=201)
async def upload_asset(pid: str, request: Request, filename: str = "upload", role: Literal["media", "logo"] = "media") -> dict:
    """Raw-body upload (the browser sends the File as the request body so progress is real)."""
    s = get_settings()
    await run_in_threadpool(projects.get_project, pid)  # 404 early
    limit = max(s.max_video_mb, s.max_image_mb) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AppError("bad_upload", f"File is larger than the {max(s.max_video_mb, s.max_image_mb)} MB limit.", status=413)
    tmp_dir = s.data_dir / "tmp" / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}.part"
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise AppError("bad_upload", "File exceeds the upload size limit.", status=413)
                f.write(chunk)
        if size == 0:
            raise AppError("bad_upload", "The uploaded file is empty.")
        return await run_in_threadpool(projects.add_asset, pid, tmp, filename, role)
    finally:
        tmp.unlink(missing_ok=True)


class PatchAsset(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    classification: Literal["screenshot", "photo"] | None = None


class Reorder(BaseModel):
    ids: list[str]


@app.patch("/api/projects/{pid}/assets/{aid}")
def patch_asset(pid: str, aid: str, body: PatchAsset) -> dict:
    return projects.update_asset(pid, aid, label=body.label, classification=body.classification)


@app.delete("/api/projects/{pid}/assets/{aid}", status_code=204)
def delete_asset(pid: str, aid: str) -> None:
    projects.delete_asset(pid, aid)


@app.post("/api/projects/{pid}/assets/reorder")
def reorder_assets(pid: str, body: Reorder) -> list[dict]:
    return projects.reorder_assets(pid, body.ids)


@app.get("/api/projects/{pid}/assets/{aid}/content")
def asset_content(pid: str, aid: str, variant: Literal["original", "thumb", "master"] = "original"):
    path, mime = projects.asset_file(pid, aid, variant)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/projects/{pid}/assets/{aid}/frames/{idx}")
def asset_frame(pid: str, aid: str, idx: int):
    return FileResponse(projects.frame_file(pid, aid, idx), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------
# Storyboard
# ---------------------------------------------------------------------------


def _timeline(pid: str, sb) -> dict:
    provider = get_provider()
    speech = cached_speech(pid, sb, provider)
    tl = resolve(sb, speech)
    d = tl.as_dict()
    d.pop("captions", None)
    d["measured_scenes"] = sorted(speech)
    return d


@app.get("/api/projects/{pid}/storyboard")
def get_storyboard(pid: str) -> dict:
    state = projects.storyboard_state(pid)
    if state.get("storyboard"):
        sb = projects.get_storyboard(pid)
        state["timeline"] = _timeline(pid, sb)
    state["revisions"] = projects.list_revisions(pid)
    return state


class PatchStoryboard(BaseModel):
    storyboard: dict
    base_revision: int


@app.patch("/api/projects/{pid}/storyboard")
def patch_storyboard(pid: str, body: PatchStoryboard) -> dict:
    projects.save_storyboard(pid, body.storyboard, base_revision=body.base_revision, source="user", note="Edited in review")
    return get_storyboard(pid)


class Revert(BaseModel):
    revision: int
    base_revision: int


@app.post("/api/projects/{pid}/storyboard/revert")
def revert_storyboard(pid: str, body: Revert) -> dict:
    projects.revert_storyboard(pid, body.revision, body.base_revision)
    return get_storyboard(pid)


@app.get("/api/projects/{pid}/storyboard/revisions/{revision}")
def get_revision(pid: str, revision: int) -> dict:
    sb = projects.get_storyboard(pid, revision)
    return sb.model_dump()  # type: ignore[union-attr]


@app.post("/api/projects/{pid}/storyboard/generate", status_code=202)
def generate_story(pid: str) -> dict:
    p = projects.get_project(pid)
    if not p["readiness"]["ready"]:
        raise AppError("not_ready", " ".join(p["readiness"]["problems"]))
    job, created = jobs.enqueue(pid, "generate_story", {}, dedupe_key=f"{pid}:story")
    return {"job": job, "created": created}


@app.get("/api/projects/{pid}/estimate")
def estimate(pid: str) -> dict:
    """Estimated AI cost of generating the story (before any paid call)."""
    from .pipeline import preflight_story_estimate  # noqa: PLC0415

    provider = get_provider()
    p = projects.get_project(pid)
    if provider.name != "openrouter":
        return {"provider": provider.name, "estimate_usd": None, "note": "Fixture mode makes no paid calls."}
    with db.connect() as conn:
        assets = [dict(r) for r in conn.execute("SELECT * FROM assets WHERE project_id = ?", (pid,))]
    try:
        est = preflight_story_estimate(provider, pid, assets)
    except AppError as e:
        return {"provider": provider.name, "estimate_usd": None, "error": e.as_dict()}
    c = costs.project_costs(pid)
    return {"provider": provider.name, "estimate_usd": est, "budget_usd": p["ai_budget_usd"],
            "spent_usd": c["reported_usd"] + c["estimated_unreported_usd"],
            "note": "Conservative upper bound from OpenRouter catalog prices (max across endpoints), including one repair request."}


class RegenerateScene(BaseModel):
    base_revision: int
    instruction: str = Field(default="", max_length=400)


@app.post("/api/projects/{pid}/scenes/{scene_id}/regenerate", status_code=202)
def regenerate_scene(pid: str, scene_id: str, body: RegenerateScene) -> dict:
    sb = projects.get_storyboard(pid)
    if sb.revision != body.base_revision:  # type: ignore[union-attr]
        raise AppError("stale_revision", "The storyboard changed; reload before regenerating this scene.",
                       detail={"current_revision": sb.revision})  # type: ignore[union-attr]
    if not any(s.id == scene_id for s in sb.scenes):  # type: ignore[union-attr]
        raise AppError("not_found", "Scene not found.")
    job, created = jobs.enqueue(pid, "regenerate_scene", {"scene_id": scene_id, "base_revision": body.base_revision,
                                                          "instruction": body.instruction},
                                revision=body.base_revision, dedupe_key=f"{pid}:scene:{scene_id}")
    return {"job": job, "created": created}


class AcceptScene(BaseModel):
    job_id: str
    base_revision: int
    force: bool = False


@app.post("/api/projects/{pid}/scenes/{scene_id}/accept")
def accept_scene(pid: str, scene_id: str, body: AcceptScene) -> dict:
    job = jobs.get_job(body.job_id, pid)
    res = job.get("result") or {}
    if job["kind"] != "regenerate_scene" or job["status"] != "story_ready" or res.get("scene_id") != scene_id:
        raise AppError("conflict", "No finished proposal for this scene.")
    current = projects.get_storyboard(pid)
    base = projects.get_storyboard(pid, res["base_revision"])
    idx = next((i for i, s in enumerate(current.scenes) if s.id == scene_id), None)  # type: ignore[union-attr]
    if idx is None:
        raise AppError("conflict", "That scene was deleted after the proposal was requested.")
    base_scene = next((s for s in base.scenes if s.id == scene_id), None)  # type: ignore[union-attr]
    if not body.force and base_scene is not None and base_scene != current.scenes[idx]:  # type: ignore[union-attr]
        raise AppError("stale_revision", "You edited this scene after requesting the regeneration. Review the proposal against your edits, then accept again to replace them.",
                       detail={"requires_force": True})
    data = current.model_dump()  # type: ignore[union-attr]
    proposal = Scene.model_validate(res["proposal"])
    if idx == 0:
        proposal.transition_in = "cut"
    data["scenes"][idx] = proposal.model_dump()
    projects.save_storyboard(pid, data, base_revision=body.base_revision, source="user",
                             note=f"Accepted regenerated scene {idx + 1}")
    return get_storyboard(pid)


@app.get("/api/projects/{pid}/timeline")
def timeline(pid: str) -> dict:
    sb = projects.get_storyboard(pid)
    return _timeline(pid, sb)


# ---------------------------------------------------------------------------
# Narration / renders / jobs
# ---------------------------------------------------------------------------


@app.post("/api/projects/{pid}/narration", status_code=202)
def narrate(pid: str) -> dict:
    sb = projects.get_storyboard(pid)
    if sb.narration.mode != "tts":  # type: ignore[union-attr]
        raise AppError("conflict", "Narration is turned off for this project.")
    job, created = jobs.enqueue(pid, "narrate", {}, revision=sb.revision, dedupe_key=f"{pid}:narrate")  # type: ignore[union-attr]
    return {"job": job, "created": created}


class RenderRequest(BaseModel):
    quality: Literal["draft", "final"] = "draft"
    accept_runtime: bool = False
    without_narration: bool = False


@app.post("/api/projects/{pid}/renders", status_code=202)
def create_render(pid: str, body: RenderRequest) -> dict:
    sb = projects.get_storyboard(pid)
    blockers = export_blockers(sb)  # type: ignore[arg-type]
    if blockers:
        raise AppError("export_blocked", " ".join(b.message for b in blockers), detail=[b.as_dict() for b in blockers])
    rep = projects.validate_storyboard(pid, sb)  # type: ignore[arg-type]
    if not rep.ok:
        raise AppError("invalid_storyboard", "; ".join(i.message for i in rep.errors), detail=[i.as_dict() for i in rep.issues])
    job, created = jobs.enqueue(pid, "render", body.model_dump(), revision=sb.revision, dedupe_key=f"{pid}:render")  # type: ignore[union-attr]
    return {"job": job, "created": created}


@app.get("/api/projects/{pid}/jobs")
def project_jobs(pid: str) -> list[dict]:
    projects.get_project(pid)
    return jobs.list_jobs(pid)


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict:
    return jobs.get_job(jid)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str) -> dict:
    return jobs.request_cancel(jid)


class RetryRequest(BaseModel):
    confirm_duplicate_charge: bool = False


@app.post("/api/jobs/{jid}/retry", status_code=202)
def retry_job(jid: str, body: RetryRequest) -> dict:
    job = jobs.get_job(jid)
    return {"job": jobs.retry(jid, job["project_id"], confirm_duplicate_charge=body.confirm_duplicate_charge)}


# ---------------------------------------------------------------------------
# Exports, costs, feedback
# ---------------------------------------------------------------------------


@app.get("/api/projects/{pid}/exports")
def list_exports(pid: str) -> list[dict]:
    return projects.list_exports(pid)


@app.get("/api/projects/{pid}/exports/{eid}")
def export_file(pid: str, eid: str, file: Literal["video", "script", "captions", "storyboard", "timeline"] = "video",
                download: bool = False):
    path, mime, name = projects.export_file(pid, eid, file)
    return FileResponse(path, media_type=mime, filename=name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/projects/{pid}/costs")
def project_costs(pid: str) -> dict:
    projects.get_project(pid)
    return costs.project_costs(pid)


class Feedback(BaseModel):
    usefulness: int = Field(ge=1, le=5)
    biggest_issue: str = Field(default="", max_length=2000)
    willingness_to_pay: str = Field(default="", max_length=200)


@app.post("/api/projects/{pid}/feedback", status_code=201)
def feedback(pid: str, body: Feedback) -> dict:
    return projects.add_feedback(pid, body.usefulness, body.biggest_issue, body.willingness_to_pay)


@app.get("/api/feedback")
def list_feedback() -> list[dict]:
    return projects.list_feedback()


# ---------------------------------------------------------------------------
# Frontend (built by Vite) — served last so /api routes win.
# ---------------------------------------------------------------------------


def _frontend_dir() -> Path | None:
    env = os.environ.get("FRONTEND_DIST")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parents[2] / "frontend" / "dist")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


_dist = _frontend_dir()
if _dist is not None:
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="static-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise AppError("not_found", "Not found.")
        candidate = (_dist / full_path).resolve()
        if full_path and _dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
else:

    @app.get("/", include_in_schema=False)
    def no_frontend() -> JSONResponse:
        return JSONResponse({"message": "Demojo API is running. Build the frontend (cd frontend && npm run build) to serve the UI here."})
