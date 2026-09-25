"""Narration synthesis with caching and measured durations.

Speech is cached per project by (provider, model, voice, format, text). A
provider failure is surfaced as an error; it never silently becomes silence.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from . import db
from . import ffmpeg as ff
from .errors import AppError
from .projects import data_path, project_dir, rel_path
from .storyboard import Storyboard
from .timeline import estimate_speech_seconds


def normalize_script(text: str) -> str:
    return " ".join(text.split())


def speech_key(provider: str, model: str, voice: str, fmt: str, text: str) -> str:
    return hashlib.sha256(f"{provider}|{model}|{voice}|{fmt}|speed=1.0|{normalize_script(text)}".encode()).hexdigest()[:40]


def _lookup(pid: str, key: str) -> tuple[Path, float] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT path, duration_s FROM speech_cache WHERE key = ? AND project_id = ?", (key, pid)).fetchone()
    if row is None:
        return None
    try:
        p = data_path(row["path"])
    except AppError:
        return None
    if not p.exists():
        return None
    return p, float(row["duration_s"])


def cached_speech(pid: str, sb: Storyboard, provider) -> dict[str, tuple[float, bool, str]]:
    """Measured durations for scenes whose narration audio is already cached (no provider calls)."""
    out: dict[str, tuple[float, bool, str]] = {}
    if sb.narration.mode != "tts":
        return out
    fmt = "wav" if provider.name == "fixture" else "mp3"
    for sc in sb.scenes:
        if not sc.narration.strip():
            continue
        key = speech_key(provider.name, provider.tts_model, provider.tts_voice, fmt, sc.narration)
        hit = _lookup(pid, key)
        if hit:
            out[sc.id] = (hit[1], True, key)
    return out


def synthesize_all(provider, ledger, pid: str, sb: Storyboard, *, progress: Callable[[str, int, int], None],
                   cancel_check: Callable[[], bool]) -> tuple[dict[str, tuple[float, bool, str]], dict[str, Path], int]:
    """Ensure audio exists for every narrated scene. Returns (speech map, files, new_synth_count)."""
    speech: dict[str, tuple[float, bool, str]] = {}
    files: dict[str, Path] = {}
    if sb.narration.mode != "tts":
        return speech, files, 0
    todo = [sc for sc in sb.scenes if sc.narration.strip()]
    fmt = "wav" if provider.name == "fixture" else "mp3"
    out_dir = project_dir(pid) / "cache" / "tts"
    out_dir.mkdir(parents=True, exist_ok=True)
    new = 0
    for i, sc in enumerate(todo):
        if cancel_check():
            raise AppError("canceled", "Canceled.")
        text = normalize_script(sc.narration)
        key = speech_key(provider.name, provider.tts_model, provider.tts_voice, fmt, text)
        hit = _lookup(pid, key)
        progress(f"Narration {i + 1} of {len(todo)}" + (" (cached)" if hit else ""), i, len(todo))
        if hit is None:
            audio, ext, _gen = provider.synthesize(ledger, text)
            dest = out_dir / f"{key}.{ext}"
            tmp = out_dir / f".{key}.{uuid.uuid4().hex[:6]}.{ext}"
            tmp.write_bytes(audio)
            try:
                dur = ff.audio_duration(tmp)
            except ff.FFmpegError as e:
                tmp.unlink(missing_ok=True)
                raise AppError("narration_failed", f"Narration audio for scene {sb.scenes.index(sc) + 1} could not be decoded; retry.") from e
            expected = estimate_speech_seconds(text)
            if dur < max(0.3, expected * 0.3):
                tmp.unlink(missing_ok=True)
                raise AppError("narration_failed", f"Narration for scene {sb.scenes.index(sc) + 1} came back suspiciously short ({dur:.1f}s); retry.")
            os.replace(tmp, dest)
            with db.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO speech_cache(key, project_id, path, duration_s, provider, model, voice, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (key, pid, rel_path(dest), dur, provider.name, provider.tts_model, provider.tts_voice, db.now_iso()),
                )
            hit = (dest, dur)
            new += 1
        speech[sc.id] = (hit[1], True, key)
        files[sc.id] = hit[0]
    progress(f"Narration ready ({len(todo)} scenes)", len(todo), len(todo))
    return speech, files, new
