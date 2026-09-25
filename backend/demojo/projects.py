"""Project service: projects, assets, storyboard revisions, exports, deletion.

Clients only ever refer to assets, exports, and jobs by ID; every lookup is
scoped to the owning project, and filesystem paths are generated internally
and resolved inside DATA_DIR.
"""

from __future__ import annotations

import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import db
from .config import get_settings
from .errors import AppError, not_found
from .media import IngestError, ingest_image, ingest_logo, ingest_video, sha256_file
from .storyboard import (
    HEX_RE,
    AssetInfo,
    Storyboard,
    ValidationReport,
    export_blockers,
    validate_against_assets,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def data_path(rel: str | None) -> Path:
    if not rel:
        raise AppError("not_found", "File not found.")
    root = get_settings().data_dir
    p = (root / rel).resolve()
    if root not in p.parents and p != root:
        raise AppError("not_found", "File not found.")
    return p


def rel_path(p: Path) -> str:
    return str(p.resolve().relative_to(get_settings().data_dir))


def project_dir(pid: str) -> Path:
    if not pid.startswith("prj_") or not pid[4:].isalnum():
        raise not_found("Project")
    return get_settings().data_dir / "projects" / pid


# ---------------------------------------------------------------------------
# Project details (Describe step)
# ---------------------------------------------------------------------------


class ProjectDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(default="", max_length=60)
    description: str = Field(default="", max_length=1200)
    audience: str = Field(default="", max_length=200)
    selling_points: list[str] = Field(default_factory=lambda: ["", "", ""], max_length=3)
    cta_text: str = Field(default="", max_length=60)
    website_text: str = Field(default="", max_length=80)
    product_type: Literal["software", "physical"] = "software"
    style: Literal["clean_launch", "guided_walkthrough"] = "clean_launch"
    target_duration_s: Literal[20, 30, 45, 60] = 30
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"
    narration: bool = True
    script_notes: str = Field(default="", max_length=3000)
    workflow_notes: str = Field(default="", max_length=1500)
    accent_color: str = "#5B5BD6"
    logo_asset_id: str | None = None

    @field_validator("selling_points")
    @classmethod
    def _points(cls, v: list[str]) -> list[str]:
        v = [p[:140] for p in v][:3]
        return v + [""] * (3 - len(v))

    @field_validator("accent_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX_RE.match(v):
            raise ValueError("accent colour must be #RRGGBB")
        return v.upper()


def _project_row(conn: sqlite3.Connection, pid: str) -> dict:
    row = db.row_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())
    if row is None:
        raise not_found("Project")
    return row


def create_project(name: str | None = None) -> dict:
    s = get_settings()
    pid = new_id("prj")
    now = db.now_iso()
    details = ProjectDetails()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO projects(id, name, details_json, current_revision, ai_budget_usd, created_at, updated_at)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (pid, (name or "Untitled demo")[:80], details.model_dump_json(), s.ai_project_budget_usd, now, now),
        )
    project_dir(pid).mkdir(parents=True, exist_ok=True)
    return get_project(pid)


def list_projects() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            thumb = conn.execute(
                "SELECT id FROM assets WHERE project_id = ? AND role = 'media' ORDER BY position LIMIT 1", (d["id"],)
            ).fetchone()
            n_assets = conn.execute("SELECT COUNT(*) FROM assets WHERE project_id = ? AND role='media'", (d["id"],)).fetchone()[0]
            n_exports = conn.execute("SELECT COUNT(*) FROM exports WHERE project_id = ?", (d["id"],)).fetchone()[0]
            out.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "updated_at": d["updated_at"],
                    "created_at": d["created_at"],
                    "has_storyboard": d["current_revision"] is not None,
                    "asset_count": n_assets,
                    "export_count": n_exports,
                    "thumb_asset_id": thumb["id"] if thumb else None,
                    "details": db.loads(d["details_json"], {}),
                }
            )
        return out


def get_project(pid: str) -> dict:
    with db.connect() as conn:
        row = _project_row(conn, pid)
        assets = [asset_public(a) for a in conn.execute(
            "SELECT * FROM assets WHERE project_id = ? ORDER BY role DESC, position", (pid,)
        ).fetchall()]
        for a in assets:
            if a["kind"] == "video":
                a["frames"] = [
                    {"index": f["idx"], "t": f["t_s"]}
                    for f in conn.execute("SELECT idx, t_s FROM asset_frames WHERE asset_id = ? ORDER BY idx", (a["id"],))
                ]
    details = db.loads(row["details_json"], {})
    return {
        "id": pid,
        "name": row["name"],
        "details": details,
        "current_revision": row["current_revision"],
        "ai_budget_usd": row["ai_budget_usd"],
        "allow_unpriced": bool(row["allow_unpriced"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "assets": [a for a in assets if a["role"] == "media"],
        "logo": next((a for a in assets if a["role"] == "logo"), None),
        "readiness": readiness(details, assets),
    }


def asset_public(row: sqlite3.Row | dict) -> dict:
    r = dict(row)
    return {
        "id": r["id"],
        "role": r["role"],
        "kind": r["kind"],
        "classification": r["classification"],
        "label": r["label"],
        "original_name": r["original_name"],
        "position": r["position"],
        "size_bytes": r["size_bytes"],
        "mime": r["mime"],
        "width": r["width"],
        "height": r["height"],
        "duration_s": r["duration_s"],
        "fps": r["fps"],
        "has_audio": bool(r["has_audio"]),
        "has_alpha": bool(r["has_alpha"]),
        "sha256": r["sha256"][:12],
    }


def readiness(details: dict, assets: list[dict]) -> dict:
    media = [a for a in assets if a["role"] == "media"]
    has_video = any(a["kind"] == "video" for a in media)
    problems: list[str] = []
    if not media:
        problems.append("Upload at least one screenshot, photo, or recording.")
    if not details.get("product_name", "").strip():
        problems.append("Add the product name.")
    if len(details.get("description", "").strip()) < 20:
        problems.append("Add a short product description (at least a sentence).")
    if details.get("product_type") == "software" and not has_video and len(details.get("workflow_notes", "").strip()) < 15:
        problems.append(
            "Describe the actual workflow: screenshots alone cannot show how the product is used."
        )
    return {"ready": not problems, "problems": problems}


def update_project(pid: str, *, name: str | None = None, details: dict | None = None,
                   ai_budget_usd: float | None = None, allow_unpriced: bool | None = None) -> dict:
    with db.connect() as conn:
        with db.transaction(conn):
            row = _project_row(conn, pid)
            cur = ProjectDetails.model_validate(db.loads(row["details_json"], {}))
            new_details = cur
            if details is not None:
                merged = cur.model_dump()
                merged.update(details)
                try:
                    new_details = ProjectDetails.model_validate(merged)
                except ValidationError as e:
                    raise AppError("bad_upload", _pydantic_message(e), status=422) from e
                if new_details.logo_asset_id is not None:
                    ok = conn.execute(
                        "SELECT 1 FROM assets WHERE id = ? AND project_id = ? AND role = 'logo'", (new_details.logo_asset_id, pid)
                    ).fetchone()
                    if not ok:
                        raise AppError("not_found", "Logo not found in this project.")
            sets = ["details_json = ?", "updated_at = ?"]
            vals: list = [new_details.model_dump_json(), db.now_iso()]
            if name is not None:
                sets.append("name = ?")
                vals.append(name.strip()[:80] or "Untitled demo")
            if ai_budget_usd is not None:
                if not (0 < ai_budget_usd <= 50):
                    raise AppError("bad_upload", "Budget must be between $0.01 and $50.", status=422)
                sets.append("ai_budget_usd = ?")
                vals.append(float(ai_budget_usd))
            if allow_unpriced is not None:
                sets.append("allow_unpriced = ?")
                vals.append(1 if allow_unpriced else 0)
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", (*vals, pid))
            if details is not None and row["current_revision"] is not None:
                _sync_storyboard_settings(conn, pid, new_details)
    return get_project(pid)


def _pydantic_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


def apply_details(sb: Storyboard, d: ProjectDetails) -> Storyboard:
    data = sb.model_dump()
    data["output"]["aspect_ratio"] = d.aspect_ratio
    data["output"]["target_duration_s"] = d.target_duration_s
    data["branding"].update(
        product_name=d.product_name,
        accent_color=d.accent_color,
        logo_asset_id=d.logo_asset_id,
        cta_text=d.cta_text,
        website_text=d.website_text,
    )
    data["style"] = d.style
    data["product_type"] = d.product_type
    data["narration"]["mode"] = "tts" if d.narration else "none"
    return Storyboard.model_validate(data)


def _sync_storyboard_settings(conn: sqlite3.Connection, pid: str, d: ProjectDetails) -> None:
    rev = conn.execute("SELECT current_revision FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    row = conn.execute("SELECT json FROM storyboard_revisions WHERE project_id = ? AND revision = ?", (pid, rev)).fetchone()
    if row is None:
        return
    sb = Storyboard.model_validate_json(row["json"])
    new = apply_details(sb, d)
    if new.model_dump(exclude={"revision"}) == sb.model_dump(exclude={"revision"}):
        return
    _insert_revision(conn, pid, new, "user", "Project settings updated (aspect, duration, style, branding or narration)")


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def project_bytes(conn: sqlite3.Connection, pid: str) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM assets WHERE project_id = ?", (pid,)).fetchone()[0])


def _sniff(path: Path) -> tuple[str, str]:
    """Detect the upload type from its content (never trust the file name)."""
    with open(path, "rb") as f:
        head = f.read(64)
    if head.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[4:8] == b"ftyp":
        return "video", ".mov" if head[8:10] == b"qt" else ".mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", ".webm"
    return "unknown", ""


def add_asset(pid: str, tmp_path: Path, original_name: str, role: Literal["media", "logo"] = "media") -> dict:
    s = get_settings()
    size = tmp_path.stat().st_size
    kind, ext = _sniff(tmp_path)
    safe_name = "".join(ch for ch in (original_name or "upload") if ch.isprintable())[:120] or "upload"
    with db.connect() as conn:
        _project_row(conn, pid)
        counts = {
            r["kind"]: r["n"]
            for r in conn.execute("SELECT kind, COUNT(*) AS n FROM assets WHERE project_id = ? GROUP BY kind", (pid,))
        }
        if project_bytes(conn, pid) + size > s.max_upload_mb * 1024 * 1024:
            raise AppError("bad_upload", f"This upload would exceed the {s.max_upload_mb} MB per-project limit.")
    if role == "logo":
        if kind != "image":
            raise AppError("bad_upload", "The logo must be a PNG, JPG, or WebP image (PNG with transparency works best).")
        if size > s.max_image_mb * 1024 * 1024:
            raise AppError("bad_upload", f"Images must be {s.max_image_mb} MB or smaller.")
    elif kind == "image":
        if size > s.max_image_mb * 1024 * 1024:
            raise AppError("bad_upload", f"Images must be {s.max_image_mb} MB or smaller.")
        if counts.get("image", 0) >= s.max_images:
            raise AppError("bad_upload", f"A project can have at most {s.max_images} images.")
    elif kind == "video":
        if size > s.max_video_mb * 1024 * 1024:
            raise AppError("bad_upload", f"Recordings must be {s.max_video_mb} MB or smaller.")
        if counts.get("video", 0) >= 1:
            raise AppError("bad_upload", "The prototype supports one screen recording per project. Delete the current one first.")
    else:
        raise AppError("bad_upload", "Unsupported file type. Upload JPG, PNG, or WebP images, or an MP4, MOV, or WebM recording.")

    aid = new_id("ast")
    adir = project_dir(pid) / "assets" / aid
    adir.mkdir(parents=True, exist_ok=True)
    sha = sha256_file(tmp_path)
    try:
        if kind == "image":
            original = adir / f"original{ext}"
            shutil.move(str(tmp_path), original)
            res = (ingest_logo if role == "logo" else ingest_image)(original, adir, max_pixels=s.max_image_pixels)
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[res.fmt]
            classification = "logo" if role == "logo" else ("photo" if res.looks_like_photo else "screenshot")
            meta = dict(kind="logo" if role == "logo" else "image", classification=classification, mime=mime,
                        width=res.width, height=res.height, duration_s=None, fps=None, vcodec=None, rotation=0,
                        has_audio=0, has_alpha=1 if res.has_alpha else 0, master=res.master, thumb=res.thumb,
                        analysis=res.analysis, frames=[])
        else:
            original = adir / f"original{ext}"
            shutil.move(str(tmp_path), original)
            res = ingest_video(original, adir, max_seconds=s.max_video_seconds)
            info = res.info
            mime = {".webm": "video/webm", ".mov": "video/quicktime"}.get(ext, "video/mp4")
            meta = dict(kind="video", classification="recording", mime=mime, width=info.display_width,
                        height=info.display_height, duration_s=round(info.duration_s, 3), fps=round(info.fps, 3),
                        vcodec=info.vcodec, rotation=info.rotation, has_audio=1 if info.has_audio else 0, has_alpha=0,
                        master=None, thumb=res.poster, analysis=None, frames=res.frames)
    except IngestError as e:
        shutil.rmtree(adir, ignore_errors=True)
        raise AppError(e.code, str(e)) from e
    except Exception:
        shutil.rmtree(adir, ignore_errors=True)
        raise

    label = Path(safe_name).stem[:60] or ("Recording" if kind == "video" else "Image")
    with db.connect() as conn:
        with db.transaction(conn):
            _project_row(conn, pid)
            if role == "logo":
                old = conn.execute("SELECT id FROM assets WHERE project_id = ? AND role = 'logo'", (pid,)).fetchall()
                for o in old:
                    _remove_asset_row(conn, pid, o["id"])
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM assets WHERE project_id = ?", (pid,)).fetchone()[0]
            conn.execute(
                "INSERT INTO assets(id, project_id, role, kind, classification, label, original_name, position, sha256,"
                " size_bytes, mime, width, height, duration_s, fps, vcodec, rotation, has_audio, has_alpha, original_path,"
                " master_path, thumb_path, analysis_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, pid, role, meta["kind"], meta["classification"], label, safe_name, pos, sha, size, meta["mime"],
                 meta["width"], meta["height"], meta["duration_s"], meta["fps"], meta["vcodec"], meta["rotation"],
                 meta["has_audio"], meta["has_alpha"], rel_path(original),
                 rel_path(meta["master"]) if meta["master"] else None,
                 rel_path(meta["thumb"]) if meta["thumb"] else None,
                 rel_path(meta["analysis"]) if meta["analysis"] else None, db.now_iso()),
            )
            for fr in meta["frames"]:
                conn.execute("INSERT INTO asset_frames(asset_id, idx, t_s, path) VALUES (?,?,?,?)",
                             (aid, fr.index, fr.t, rel_path(fr.path)))
            if role == "logo":
                row = _project_row(conn, pid)
                d = ProjectDetails.model_validate(db.loads(row["details_json"], {}))
                d.logo_asset_id = aid
                conn.execute("UPDATE projects SET details_json = ?, updated_at = ? WHERE id = ?",
                             (d.model_dump_json(), db.now_iso(), pid))
                if row["current_revision"] is not None:
                    _sync_storyboard_settings(conn, pid, d)
            else:
                conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (db.now_iso(), pid))
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (aid,)).fetchone()
    out = asset_public(row)
    if out["kind"] == "video":
        out["frames"] = [{"index": f.index, "t": f.t} for f in meta["frames"]]
    return out


def get_asset_row(pid: str, aid: str) -> dict:
    with db.connect() as conn:
        row = db.row_dict(conn.execute("SELECT * FROM assets WHERE id = ? AND project_id = ?", (aid, pid)).fetchone())
    if row is None:
        raise not_found("Asset")
    return row


def asset_file(pid: str, aid: str, variant: str) -> tuple[Path, str]:
    row = get_asset_row(pid, aid)
    if variant == "original":
        return data_path(row["original_path"]), row["mime"]
    if variant == "thumb":
        return data_path(row["thumb_path"]), "image/jpeg"
    if variant == "master" and row["master_path"]:
        p = data_path(row["master_path"])
        return p, "image/png" if p.suffix == ".png" else "image/jpeg"
    raise not_found("Asset variant")


def frame_file(pid: str, aid: str, idx: int) -> Path:
    get_asset_row(pid, aid)
    with db.connect() as conn:
        row = conn.execute("SELECT path FROM asset_frames WHERE asset_id = ? AND idx = ?", (aid, idx)).fetchone()
    if row is None:
        raise not_found("Frame")
    return data_path(row["path"])


def update_asset(pid: str, aid: str, *, label: str | None = None, classification: str | None = None) -> dict:
    row = get_asset_row(pid, aid)
    sets, vals = [], []
    if label is not None:
        sets.append("label = ?")
        vals.append("".join(ch for ch in label if ch.isprintable()).strip()[:60] or row["label"])
    if classification is not None:
        if row["kind"] != "image" or classification not in ("screenshot", "photo"):
            raise AppError("bad_upload", "Images can be classified as 'screenshot' or 'photo'.", status=422)
        sets.append("classification = ?")
        vals.append(classification)
    if sets:
        with db.connect() as conn:
            conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ? AND project_id = ?", (*vals, aid, pid))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (db.now_iso(), pid))
    return asset_public(get_asset_row(pid, aid))


def reorder_assets(pid: str, ids: list[str]) -> list[dict]:
    with db.connect() as conn:
        with db.transaction(conn):
            existing = [r["id"] for r in conn.execute(
                "SELECT id FROM assets WHERE project_id = ? AND role = 'media' ORDER BY position", (pid,))]
            if sorted(existing) != sorted(ids):
                raise AppError("conflict", "Reorder must list every media asset of this project exactly once.")
            for pos, aid in enumerate(ids):
                conn.execute("UPDATE assets SET position = ? WHERE id = ? AND project_id = ?", (pos, aid, pid))
    return get_project(pid)["assets"]


def _remove_asset_row(conn: sqlite3.Connection, pid: str, aid: str) -> None:
    conn.execute("DELETE FROM assets WHERE id = ? AND project_id = ?", (aid, pid))
    shutil.rmtree(project_dir(pid) / "assets" / aid, ignore_errors=True)


def delete_asset(pid: str, aid: str) -> None:
    row = get_asset_row(pid, aid)
    sb = get_storyboard(pid, required=False)
    if sb is not None:
        used = [i + 1 for i, sc in enumerate(sb.scenes) if sc.asset_id == aid]
        if used:
            raise AppError("conflict", f"This asset is used by scene(s) {', '.join(map(str, used))}. Replace or delete those scenes first.")
    with db.connect() as conn:
        with db.transaction(conn):
            _remove_asset_row(conn, pid, aid)
            if row["role"] == "logo":
                prow = _project_row(conn, pid)
                d = ProjectDetails.model_validate(db.loads(prow["details_json"], {}))
                d.logo_asset_id = None
                conn.execute("UPDATE projects SET details_json = ? WHERE id = ?", (d.model_dump_json(), pid))
                if prow["current_revision"] is not None:
                    _sync_storyboard_settings(conn, pid, d)
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (db.now_iso(), pid))


def asset_infos(pid: str) -> dict[str, AssetInfo]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM assets WHERE project_id = ?", (pid,)).fetchall()
    return {r["id"]: AssetInfo(r["id"], r["kind"], r["width"], r["height"], r["duration_s"]) for r in rows}


# ---------------------------------------------------------------------------
# Storyboard revisions
# ---------------------------------------------------------------------------


def get_storyboard(pid: str, revision: int | None = None, *, required: bool = True) -> Storyboard | None:
    with db.connect() as conn:
        row = _project_row(conn, pid)
        rev = revision if revision is not None else row["current_revision"]
        if rev is None:
            if required:
                raise AppError("not_ready", "No storyboard yet. Generate the story first.")
            return None
        r = conn.execute("SELECT json FROM storyboard_revisions WHERE project_id = ? AND revision = ?", (pid, rev)).fetchone()
    if r is None:
        raise not_found("Storyboard revision")
    return Storyboard.model_validate_json(r["json"])


def _insert_revision(conn: sqlite3.Connection, pid: str, sb: Storyboard, source: str, note: str | None) -> Storyboard:
    cur = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM storyboard_revisions WHERE project_id = ?", (pid,)).fetchone()[0]
    new_rev = int(cur) + 1
    data = sb.model_dump()
    data["revision"] = new_rev
    data["project_id"] = pid
    sb2 = Storyboard.model_validate(data)
    conn.execute(
        "INSERT INTO storyboard_revisions(project_id, revision, json, source, note, created_at) VALUES (?,?,?,?,?,?)",
        (pid, new_rev, sb2.model_dump_json(), source, note, db.now_iso()),
    )
    conn.execute("UPDATE projects SET current_revision = ?, updated_at = ? WHERE id = ?", (new_rev, db.now_iso(), pid))
    _prune_revisions(conn, pid)
    return sb2


def _prune_revisions(conn: sqlite3.Connection, pid: str, keep: int = 60) -> None:
    """Keep recent revisions plus any referenced by jobs, exports, or AI generations."""
    conn.execute(
        """DELETE FROM storyboard_revisions WHERE project_id = ? AND revision NOT IN (
               SELECT revision FROM storyboard_revisions WHERE project_id = ? ORDER BY revision DESC LIMIT ?)
           AND source = 'user'
           AND revision NOT IN (SELECT revision FROM jobs WHERE project_id = ? AND revision IS NOT NULL)
           AND revision NOT IN (SELECT revision FROM exports WHERE project_id = ?)""",
        (pid, pid, keep, pid, pid),
    )


def validate_storyboard(pid: str, sb: Storyboard) -> ValidationReport:
    return validate_against_assets(sb, asset_infos(pid))


def save_storyboard(pid: str, data: dict, *, base_revision: int | None, source: str = "user",
                    note: str | None = None) -> tuple[Storyboard, ValidationReport]:
    try:
        data = dict(data)
        data["project_id"] = pid
        sb = Storyboard.model_validate(data)
    except ValidationError as e:
        raise AppError("invalid_storyboard", _pydantic_message(e), detail=e.errors(include_url=False, include_context=False)) from e
    rep = validate_storyboard(pid, sb)
    if not rep.ok:
        raise AppError("invalid_storyboard", "; ".join(i.message for i in rep.errors), detail=[i.as_dict() for i in rep.issues])
    with db.connect() as conn:
        with db.transaction(conn):
            row = _project_row(conn, pid)
            if base_revision is not None and row["current_revision"] != base_revision:
                raise AppError(
                    "stale_revision",
                    f"The storyboard changed (now revision {row['current_revision']}); your edit was based on {base_revision}. Reload to continue.",
                    detail={"current_revision": row["current_revision"]},
                )
            if base_revision is None and row["current_revision"] is not None and source == "user":
                raise AppError("stale_revision", "base_revision is required when editing an existing storyboard.")
            sb2 = _insert_revision(conn, pid, sb, source, note)
    return sb2, rep


def list_revisions(pid: str) -> list[dict]:
    with db.connect() as conn:
        _project_row(conn, pid)
        rows = conn.execute(
            "SELECT revision, source, note, created_at FROM storyboard_revisions WHERE project_id = ? ORDER BY revision DESC LIMIT 40",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]


def revert_storyboard(pid: str, revision: int, base_revision: int) -> Storyboard:
    old = get_storyboard(pid, revision)
    data = old.model_dump()  # type: ignore[union-attr]
    sb, _ = save_storyboard(pid, data, base_revision=base_revision, source="revert", note=f"Reverted to revision {revision}")
    return sb


def summarize_changes(old: Storyboard | None, new: Storyboard) -> list[str]:
    """Human-readable list of material differences between two storyboards."""
    if old is None:
        return []
    out: list[str] = []
    if old.output.aspect_ratio != new.output.aspect_ratio:
        out.append(f"Aspect ratio {old.output.aspect_ratio} → {new.output.aspect_ratio}")
    if old.output.target_duration_s != new.output.target_duration_s:
        out.append(f"Target duration {old.output.target_duration_s}s → {new.output.target_duration_s}s")
    if old.style != new.style:
        out.append(f"Style changed to {new.style.replace('_', ' ')}")
    if old.narration.mode != new.narration.mode:
        out.append("Narration turned " + ("on" if new.narration.mode == "tts" else "off"))
    if old.branding != new.branding:
        out.append("Branding (name, colour, logo, CTA or website) changed")
    old_ids = [s.id for s in old.scenes]
    new_ids = [s.id for s in new.scenes]
    old_map = {s.id: s for s in old.scenes}
    for i, sc in enumerate(new.scenes):
        if sc.id not in old_map:
            out.append(f"Scene {i + 1} added ({sc.role})")
            continue
        o = old_map[sc.id]
        diffs = []
        if o.headline != sc.headline or o.subline != sc.subline:
            diffs.append("on-screen text")
        if o.narration != sc.narration:
            diffs.append("narration")
        if o.asset_id != sc.asset_id or o.source_kind != sc.source_kind:
            diffs.append("media")
        if (o.clip_in_s, o.clip_out_s, o.planned_duration_s) != (sc.clip_in_s, sc.clip_out_s, sc.planned_duration_s):
            diffs.append("timing")
        if (o.focus_region, o.focal_point, o.highlight, o.fit_mode, o.motion, o.transition_in) != (
            sc.focus_region, sc.focal_point, sc.highlight, sc.fit_mode, sc.motion, sc.transition_in
        ):
            diffs.append("framing/motion")
        if [(c.text, c.status) for c in o.claims] != [(c.text, c.status) for c in sc.claims]:
            diffs.append("claims")
        if diffs:
            out.append(f"Scene {i + 1}: {', '.join(diffs)} changed")
    removed = [sid for sid in old_ids if sid not in new_ids]
    if removed:
        out.append(f"{len(removed)} scene(s) removed")
    common_old = [s for s in old_ids if s in new_ids]
    common_new = [s for s in new_ids if s in old_ids]
    if common_old != common_new:
        out.append("Scenes reordered")
    return out


def storyboard_state(pid: str) -> dict:
    """Current storyboard plus validation, blockers, and change summaries for the Review step."""
    sb = get_storyboard(pid, required=False)
    if sb is None:
        return {"storyboard": None}
    rep = validate_storyboard(pid, sb)
    blockers = export_blockers(sb)
    with db.connect() as conn:
        last_export = conn.execute(
            "SELECT revision, created_at, quality FROM exports WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (pid,)
        ).fetchone()
        last_ai = conn.execute(
            "SELECT revision FROM storyboard_revisions WHERE project_id = ? AND source IN ('openrouter','fixture') ORDER BY revision DESC LIMIT 1",
            (pid,),
        ).fetchone()
    since_export = None
    if last_export is not None:
        try:
            old = get_storyboard(pid, last_export["revision"])
            since_export = {"revision": last_export["revision"], "changes": summarize_changes(old, sb)}
        except AppError:
            since_export = None
    since_ai = None
    if last_ai is not None and last_ai["revision"] != sb.revision:
        try:
            since_ai = {"revision": last_ai["revision"], "changes": summarize_changes(get_storyboard(pid, last_ai["revision"]), sb)}
        except AppError:
            since_ai = None
    return {
        "storyboard": sb.model_dump(),
        "issues": [i.as_dict() for i in rep.issues],
        "blockers": [i.as_dict() for i in blockers],
        "changes_since_export": since_export,
        "changes_since_ai_draft": since_ai,
    }


# ---------------------------------------------------------------------------
# Exports, feedback, deletion
# ---------------------------------------------------------------------------


def list_exports(pid: str) -> list[dict]:
    with db.connect() as conn:
        _project_row(conn, pid)
        rows = conn.execute("SELECT * FROM exports WHERE project_id = ? ORDER BY created_at DESC", (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = db.loads(d.pop("meta_json"), {})
        d.pop("dir", None)
        d["has_audio"] = bool(d["has_audio"])
        out.append(d)
    return out


EXPORT_FILES = {
    "video": ("video.mp4", "video/mp4"),
    "script": ("script.txt", "text/plain; charset=utf-8"),
    "captions": ("captions.srt", "application/x-subrip; charset=utf-8"),
    "storyboard": ("storyboard.json", "application/json"),
    "timeline": ("timeline.json", "application/json"),
}


def export_file(pid: str, eid: str, kind: str) -> tuple[Path, str, str]:
    if kind not in EXPORT_FILES:
        raise not_found("Export file")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id = ? AND project_id = ?", (eid, pid)).fetchone()
        prow = _project_row(conn, pid)
    if row is None:
        raise not_found("Export")
    name, mime = EXPORT_FILES[kind]
    p = data_path(row["dir"]) / name
    if not p.exists():
        raise not_found("Export file")
    slug = "".join(ch if ch.isalnum() else "-" for ch in prow["name"].lower()).strip("-")[:40] or "demo"
    download = f"{slug}-{row['quality']}-r{row['revision']}{Path(name).suffix}"
    return p, mime, download


def add_feedback(pid: str | None, usefulness: int, biggest_issue: str, willingness_to_pay: str) -> dict:
    if not (1 <= usefulness <= 5):
        raise AppError("bad_upload", "Usefulness must be 1–5.", status=422)
    fid = new_id("fbk")
    with db.connect() as conn:
        if pid is not None:
            _project_row(conn, pid)
        conn.execute(
            "INSERT INTO feedback(id, project_id, usefulness, biggest_issue, willingness_to_pay, created_at) VALUES (?,?,?,?,?,?)",
            (fid, pid, usefulness, biggest_issue.strip()[:2000], willingness_to_pay.strip()[:200], db.now_iso()),
        )
    return {"id": fid}


def list_feedback() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY created_at DESC")]


def delete_project(pid: str, wait_s: float = 12.0) -> None:
    """Cancel pending work, then remove DB rows and local media for this project only."""
    with db.connect() as conn:
        _project_row(conn, pid)
        conn.execute(
            "UPDATE jobs SET status = 'canceled', finished_at = ?, error_code = 'canceled', error_message = 'Project deleted'"
            " WHERE project_id = ? AND status = 'queued'",
            (db.now_iso(), pid),
        )
        conn.execute(
            f"UPDATE jobs SET cancel_requested = 1 WHERE project_id = ? AND status IN ({','.join('?' * len(db.RUNNING_STATUSES))})",
            (pid, *db.RUNNING_STATUSES),
        )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        with db.connect() as conn:
            running = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE project_id = ? AND status IN ({','.join('?' * len(db.RUNNING_STATUSES))})",
                (pid, *db.RUNNING_STATUSES),
            ).fetchone()[0]
        if not running:
            break
        time.sleep(0.3)
    with db.connect() as conn:
        with db.transaction(conn):
            shas = [r["sha256"] for r in conn.execute("SELECT sha256 FROM assets WHERE project_id = ?", (pid,))]
            conn.execute("DELETE FROM provider_calls WHERE project_id = ?", (pid,))
            conn.execute("UPDATE feedback SET project_id = NULL WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            for sha in shas:
                still = conn.execute("SELECT 1 FROM assets WHERE sha256 = ? LIMIT 1", (sha,)).fetchone()
                if not still:
                    conn.execute("DELETE FROM analyses WHERE asset_sha = ?", (sha,))
    shutil.rmtree(project_dir(pid), ignore_errors=True)
