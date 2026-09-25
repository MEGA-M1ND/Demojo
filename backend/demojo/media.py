"""Media ingest: validation, normalisation, thumbnails, timestamped frames."""

from __future__ import annotations

import hashlib
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import ffmpeg as ff

IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
MAX_ANALYSIS_FRAMES = 24


class IngestError(ValueError):
    """Upload rejected; message is safe and actionable for the user."""

    def __init__(self, message: str, code: str = "bad_upload"):
        super().__init__(message)
        self.code = code


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@dataclass
class ImageIngest:
    master: Path
    thumb: Path
    analysis: Path
    width: int
    height: int
    has_alpha: bool
    fmt: str
    looks_like_photo: bool


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        alpha = img.convert("RGBA").getchannel("A")
        lo, _ = alpha.getextrema()
        return lo < 250
    return False


def ingest_image(src: Path, out_dir: Path, *, max_pixels: int) -> ImageIngest:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(src) as probe:
                fmt = probe.format or ""
                w0, h0 = probe.size
                if fmt not in IMAGE_FORMATS:
                    raise IngestError(f"Unsupported image format {fmt or 'unknown'}. Upload JPG, PNG, or WebP.")
                if w0 * h0 > max_pixels:
                    raise IngestError(
                        f"Image is {w0}×{h0} ({w0 * h0 / 1e6:.0f} MP). The limit is {max_pixels / 1e6:.0f} MP; resize it and try again."
                    )
                if min(w0, h0) < 64:
                    raise IngestError("Image is too small (minimum 64 px on each side).")
                probe.verify()
            img = Image.open(src)
            img.load()
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise IngestError(f"The image could not be decoded ({type(e).__name__}). It may be corrupt or truncated.") from e

    exif = img.getexif()
    looks_like_photo = bool(exif.get(0x010F) or exif.get(0x0110)) or fmt == "JPEG"
    img = ImageOps.exif_transpose(img) or img
    alpha = _has_alpha(img)
    if alpha:
        master_img = img.convert("RGBA")
        master = out_dir / "master.png"
        master_img.save(master, optimize=False)
    else:
        master_img = img.convert("RGB")
        if fmt == "JPEG":
            master = out_dir / "master.jpg"
            master_img.save(master, quality=95, subsampling=0)
        else:
            master = out_dir / "master.png"
            master_img.save(master)
    flat = master_img
    if alpha:
        bg = Image.new("RGB", master_img.size, (255, 255, 255))
        bg.paste(master_img, (0, 0), master_img)
        flat = bg
    thumb = out_dir / "thumb.jpg"
    t = flat.copy()
    t.thumbnail((640, 640), Image.LANCZOS)
    t.save(thumb, quality=85)
    analysis = out_dir / "analysis.jpg"
    a = flat.copy()
    a.thumbnail((1024, 1024), Image.LANCZOS)
    a.save(analysis, quality=85)
    return ImageIngest(master, thumb, analysis, master_img.width, master_img.height, alpha, fmt, looks_like_photo)


def ingest_logo(src: Path, out_dir: Path, *, max_pixels: int) -> ImageIngest:
    res = ingest_image(src, out_dir, max_pixels=max_pixels)
    # Logos are drawn with alpha; store an RGBA PNG master regardless of source.
    img = Image.open(res.master).convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox and res.has_alpha:
        img = img.crop(bbox)
    master = out_dir / "logo.png"
    img.save(master)
    if res.master != master:
        res.master.unlink(missing_ok=True)
    res.master = master
    res.width, res.height = img.size
    return res


@dataclass
class VideoFrame:
    index: int
    t: float
    path: Path


@dataclass
class VideoIngest:
    info: ff.MediaInfo
    poster: Path
    frames: list[VideoFrame] = field(default_factory=list)


def validate_video(src: Path, *, max_seconds: int, timeout: float = 120) -> ff.MediaInfo:
    try:
        info = ff.probe(src, timeout=timeout)
    except ff.FFmpegError as e:
        raise IngestError("The recording could not be read. Export it again as MP4 (H.264) and retry.", "unavailable_codec") from e
    if not info.has_video:
        raise IngestError("The file has no video stream.", "bad_upload")
    if info.format_name not in ff.ALLOWED_VIDEO_CONTAINERS:
        raise IngestError(f"Unsupported container '{info.format_name}'. Upload MP4, MOV, or WebM.", "bad_upload")
    if info.vcodec not in ff.ALLOWED_VIDEO_CODECS:
        raise IngestError(
            f"Video codec '{info.vcodec or 'unknown'}' is not supported. Re-export as H.264 MP4 (most screen recorders offer this).",
            "unavailable_codec",
        )
    if info.duration_s <= 0.5:
        raise IngestError("The recording is too short (under 0.5 s).")
    if info.duration_s > max_seconds + 0.5:
        raise IngestError(f"The recording is {info.duration_s / 60:.1f} min long; the limit is {max_seconds // 60} minutes. Trim it and retry.")
    if info.display_width < 64 or info.display_height < 64:
        raise IngestError("The recording's dimensions are too small.")
    if info.width * info.height > 4096 * 2304:
        raise IngestError("Recordings above 4K resolution are not supported in the prototype.")
    return info


def _sample_times(duration: float, scene_times: list[float]) -> list[float]:
    n_uniform = min(16, max(4, math.ceil(duration / 3)))
    uniform = [(i + 0.5) * duration / n_uniform for i in range(n_uniform)]
    spacing = max(0.6, duration / 48)
    chosen: list[float] = []
    for t in [*scene_times, *uniform]:
        t = min(max(0.0, t), max(0.0, duration - 0.08))
        if all(abs(t - c) >= spacing for c in chosen):
            chosen.append(t)
        if len(chosen) >= MAX_ANALYSIS_FRAMES:
            break
    return sorted(chosen)


def ingest_video(src: Path, out_dir: Path, *, max_seconds: int) -> VideoIngest:
    out_dir.mkdir(parents=True, exist_ok=True)
    info = validate_video(src, max_seconds=max_seconds)
    dur = info.duration_s
    poster = out_dir / "poster.jpg"
    try:
        ff.extract_frame(src, min(1.0, dur / 3), poster, max_side=960)
        # Also prove the tail decodes (truncated uploads fail here).
        tail = out_dir / "tail_check.jpg"
        ff.extract_frame(src, max(0.0, dur - 0.5), tail, max_side=160)
        tail.unlink(missing_ok=True)
    except ff.FFmpegError as e:
        raise IngestError("The recording could not be decoded end-to-end; it may be corrupt or use an unsupported codec profile. Re-export as H.264 MP4.", "unavailable_codec") from e
    try:
        scenes = ff.scene_change_times(src, start_offset=info.start_time, timeout=max(60, dur * 2))
    except Exception:
        scenes = []
    frames: list[VideoFrame] = []
    for i, t in enumerate(_sample_times(dur, scenes)):
        p = out_dir / f"frame_{i:02d}.jpg"
        try:
            ff.extract_frame(src, t, p, max_side=768)
        except ff.FFmpegError:
            continue
        frames.append(VideoFrame(index=len(frames), t=round(t, 3), path=p))
    if not frames:
        raise IngestError("No frames could be extracted from the recording.", "unavailable_codec")
    return VideoIngest(info=info, poster=poster, frames=frames)
