"""Thin, safe wrappers around ffmpeg/ffprobe.

* Always argument arrays; never ``shell=True``.
* Only generated absolute paths are passed; user text never appears in args.
* Every process has a timeout and can be cancelled cooperatively.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# Inputs we read are always local files; forbid network/nested protocols.
SAFE_INPUT = ["-protocol_whitelist", "file,pipe"]

ALLOWED_VIDEO_CONTAINERS = {"mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"}
ALLOWED_VIDEO_CODECS = {"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "prores"}


class Canceled(Exception):
    """Raised when a cooperative cancellation request is observed."""


class FFmpegError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stderr_tail = stderr_tail
        self.returncode = returncode


CancelCheck = Callable[[], bool]


def _check_path(p: Path | str) -> str:
    s = str(p)
    if not s.startswith("/"):
        raise ValueError("ffmpeg paths must be absolute internal paths")
    return s


def run(args: list[str], *, timeout: float, cancel_check: CancelCheck | None = None, capture_stdout: bool = False) -> bytes:
    """Run ffmpeg/ffprobe with a timeout and cooperative cancellation."""
    if not args or args[0] not in (FFMPEG, FFPROBE):
        raise ValueError("only ffmpeg/ffprobe may be executed")
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    out_chunks: list[bytes] = []
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                if out:
                    out_chunks.append(out)
                break
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    proc.kill()
                    proc.communicate()
                    raise Canceled() from None
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise FFmpegError(f"ffmpeg timed out after {timeout:.0f}s") from None
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace")[-2000:]
        raise FFmpegError(f"ffmpeg exited with code {proc.returncode}", tail, proc.returncode)
    return b"".join(out_chunks)


def ffprobe_json(path: Path | str, timeout: float = 60) -> dict:
    args = [
        FFPROBE, "-v", "error", *SAFE_INPUT, "-print_format", "json",
        "-show_format", "-show_streams", _check_path(path),
    ]  # fmt: skip
    out = run(args, timeout=timeout, capture_stdout=True)
    return json.loads(out.decode("utf-8", "replace") or "{}")


@dataclass
class MediaInfo:
    format_name: str
    duration_s: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    rotation: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    pix_fmt: str = ""
    nb_frames: int | None = None
    start_time: float = 0.0

    @property
    def display_width(self) -> int:
        return self.height if self.rotation in (90, 270) else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation in (90, 270) else self.height


def _rotation(stream: dict) -> int:
    rot = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rot = int(float(tags["rotate"]))
        except ValueError:
            rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = stream.get(key)
        if val and val != "0/0":
            try:
                f = float(Fraction(val))
                if f > 0:
                    return f
            except (ValueError, ZeroDivisionError):
                continue
    return 0.0


def probe(path: Path | str, timeout: float = 60) -> MediaInfo:
    data = ffprobe_json(path, timeout=timeout)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for src in (fmt.get("duration"), (v or {}).get("duration"), (a or {}).get("duration")):
        try:
            if src is not None and float(src) > 0:
                duration = float(src)
                break
        except ValueError:
            continue
    info = MediaInfo(
        format_name=fmt.get("format_name", ""),
        duration_s=duration,
        has_video=v is not None,
        has_audio=a is not None,
    )
    try:
        info.start_time = float(fmt.get("start_time") or 0.0)
    except ValueError:
        info.start_time = 0.0
    if v is not None:
        info.width = int(v.get("width") or 0)
        info.height = int(v.get("height") or 0)
        info.rotation = _rotation(v)
        info.fps = _fps(v)
        info.vcodec = v.get("codec_name", "")
        info.pix_fmt = v.get("pix_fmt", "")
        try:
            info.nb_frames = int(v["nb_frames"]) if v.get("nb_frames") else None
        except ValueError:
            info.nb_frames = None
    if a is not None:
        info.acodec = a.get("codec_name", "")
    return info


def audio_duration(path: Path | str, timeout: float = 30) -> float:
    info = probe(path, timeout=timeout)
    if not info.has_audio or info.duration_s <= 0:
        raise FFmpegError("audio file has no decodable audio stream")
    return info.duration_s


def decode_check(path: Path | str, *, timeout: float, cancel_check: CancelCheck | None = None) -> None:
    """Full decode pass; raises FFmpegError if any decode error is reported."""
    args = [FFMPEG, "-v", "error", "-nostdin", *SAFE_INPUT, "-i", _check_path(path), "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if cancel_check and cancel_check():
        raise Canceled()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or err:
        raise FFmpegError("decode check failed", err[-2000:], proc.returncode)


def extract_frame(src: Path | str, t: float, dest: Path | str, *, max_side: int, timeout: float = 60) -> None:
    """Extract one display-oriented frame at t seconds into a JPEG."""
    vf = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease"
    args = [
        FFMPEG, "-v", "error", "-nostdin", "-y", "-ss", f"{max(t, 0):.3f}", *SAFE_INPUT,
        "-i", _check_path(src), "-frames:v", "1", "-vf", vf, "-q:v", "3", _check_path(dest),
    ]  # fmt: skip
    run(args, timeout=timeout)
    if not Path(dest).exists() or Path(dest).stat().st_size == 0:
        raise FFmpegError(f"could not extract a frame at {t:.2f}s")


def scene_change_times(
    src: Path | str, *, start_offset: float = 0.0, threshold: float = 0.3, timeout: float = 300, limit: int = 60
) -> list[float]:
    """Candidate scene-change timestamps, normalised to zero-based seconds."""
    args = [
        FFMPEG, "-v", "info", "-nostdin", *SAFE_INPUT, "-i", _check_path(src), "-an",
        "-vf", f"scale=320:-2,select='gt(scene\\,{threshold:.2f})',showinfo", "-f", "null", "-",
    ]  # fmt: skip
    proc = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    times: list[float] = []
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        if "showinfo" in line and "pts_time:" in line:
            try:
                val = line.split("pts_time:")[1].split()[0]
                times.append(max(0.0, float(val) - start_offset))
            except (IndexError, ValueError):
                continue
        if len(times) >= limit:
            break
    return times
