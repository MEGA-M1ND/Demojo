"""Resolve a storyboard + measured narration into an integer-frame timeline.

The resulting manifest is the single source of truth for the video, the
narration track, the captions, and the runtime shown to the user.

Rules:
* Narration is never truncated: a scene is always at least
  speech_offset + speech + tail frames long.
* Dissolves overlap adjacent scenes by DISSOLVE_FRAMES; the speech offset of
  a scene is at least the overlap, so narration never starts under the
  previous scene and never overlaps the next scene's narration.
* Clips shorter than their scene hold their final frame; they never loop.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from .storyboard import FPS, Scene, Storyboard

LEAD_IN_FRAMES = 8  # ~0.27 s before narration starts
TAIL_FRAMES = 14  # ~0.47 s breathing room after narration
DISSOLVE_FRAMES = 12  # 0.4 s
MIN_SCENE_FRAMES = 45  # 1.5 s; must stay >= 3 * DISSOLVE_FRAMES
WORDS_PER_SECOND_ESTIMATE = 2.6


def estimate_speech_seconds(text: str) -> float:
    words = len(text.split())
    if words == 0:
        return 0.0
    return words / WORDS_PER_SECOND_ESTIMATE + 0.3


def runtime_tolerance_s(target_s: float) -> float:
    return max(1.5, 0.08 * target_s)


@dataclass
class SpeechPlacement:
    start_frame: int  # absolute timeline frame where speech starts
    duration_s: float  # measured (or estimated) seconds
    end_frame: int  # absolute frame by which speech has ended (ceil)
    measured: bool
    audio_key: str | None = None


@dataclass
class TimelineScene:
    scene_id: str
    index: int
    start_frame: int
    n_frames: int
    transition_in: str
    overlap_frames: int
    clip_frames: int | None  # frames taken from the clip before holding
    hold_frames: int
    speech: SpeechPlacement | None

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.n_frames


@dataclass
class Caption:
    index: int
    start_s: float
    end_s: float
    text: str
    scene_id: str
    approximate: bool


@dataclass
class Timeline:
    fps: int
    scenes: list[TimelineScene]
    total_frames: int
    requested_s: float
    all_speech_measured: bool
    captions: list[Caption] = field(default_factory=list)

    @property
    def computed_s(self) -> float:
        return self.total_frames / self.fps

    @property
    def over_target(self) -> bool:
        return self.computed_s > self.requested_s + runtime_tolerance_s(self.requested_s)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["computed_s"] = round(self.computed_s, 3)
        d["over_target"] = self.over_target
        d["tolerance_s"] = runtime_tolerance_s(self.requested_s)
        return d


def _clip_frames(sc: Scene) -> int | None:
    if sc.source_kind != "clip" or sc.clip_in_s is None or sc.clip_out_s is None:
        return None
    return max(1, int(round((sc.clip_out_s - sc.clip_in_s) * FPS)))


def resolve(sb: Storyboard, speech: dict[str, tuple[float, bool, str | None]] | None = None) -> Timeline:
    """Build the timeline.

    speech maps scene_id -> (duration_s, measured, audio_key). Scenes with
    narration but no entry get a words-per-second estimate (measured=False).
    """
    speech = speech or {}
    tl_scenes: list[TimelineScene] = []
    cursor = 0
    use_tts = sb.narration.mode == "tts"
    all_measured = True
    for i, sc in enumerate(sb.scenes):
        overlap = DISSOLVE_FRAMES if (i > 0 and sc.transition_in == "dissolve") else 0
        start = cursor - overlap
        planned = int(round(sc.planned_duration_s * FPS))
        clip_frames = _clip_frames(sc)
        base = clip_frames if clip_frames is not None else planned
        placement: SpeechPlacement | None = None
        need = 0
        if use_tts and sc.narration.strip():
            if sc.id in speech:
                dur, measured, key = speech[sc.id]
            else:
                dur, measured, key = estimate_speech_seconds(sc.narration), False, None
            all_measured = all_measured and measured
            offset = max(LEAD_IN_FRAMES, overlap)
            speech_frames = int(math.ceil(dur * FPS))
            need = offset + speech_frames + TAIL_FRAMES
            placement = SpeechPlacement(
                start_frame=start + offset,
                duration_s=dur,
                end_frame=start + offset + speech_frames,
                measured=measured,
                audio_key=key,
            )
        n = max(base, need, MIN_SCENE_FRAMES)
        hold = 0
        if clip_frames is not None:
            clip_frames = min(clip_frames, n)
            hold = n - clip_frames
        tl_scenes.append(
            TimelineScene(
                scene_id=sc.id,
                index=i,
                start_frame=start,
                n_frames=n,
                transition_in="dissolve" if overlap else "cut",
                overlap_frames=overlap,
                clip_frames=clip_frames,
                hold_frames=hold,
                speech=placement,
            )
        )
        cursor = start + n
    tl = Timeline(
        fps=FPS,
        scenes=tl_scenes,
        total_frames=cursor,
        requested_s=float(sb.output.target_duration_s),
        all_speech_measured=all_measured,
    )
    tl.captions = build_captions(sb, tl)
    return tl


_SPLIT_RE = re.compile(r"(?<=[.!?;:])\s+|(?<=,)\s+(?=\S{3,})")


def _phrases(text: str, max_chars: int = 84) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(text.strip()) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 1 + len(p) <= max_chars and not out[-1].endswith((".", "!", "?")):
            out[-1] = f"{out[-1]} {p}"
        elif len(p) > max_chars:
            words, cur = p.split(), ""
            for w in words:
                if cur and len(cur) + 1 + len(w) > max_chars:
                    out.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            if cur:
                out.append(cur)
        else:
            out.append(p)
    return out


def build_captions(sb: Storyboard, tl: Timeline) -> list[Caption]:
    """Scene-level captions; phrase timing inside a scene is proportional (approximate)."""
    caps: list[Caption] = []
    fps = tl.fps
    for ts, sc in zip(tl.scenes, sb.scenes, strict=True):
        text = " ".join(sc.narration.split())
        if not text:
            continue
        if ts.speech is not None:
            start = ts.speech.start_frame / fps
            end = start + ts.speech.duration_s
        else:
            # Narration off: show the scene text across the visible part of the scene.
            start = (ts.start_frame + ts.overlap_frames) / fps
            end = ts.end_frame / fps - 0.2
        phrases = _phrases(text)
        total_chars = sum(len(p) for p in phrases) or 1
        t = start
        for j, p in enumerate(phrases):
            span = (end - start) * len(p) / total_chars
            p_end = end if j == len(phrases) - 1 else t + span
            caps.append(
                Caption(
                    index=len(caps) + 1,
                    start_s=round(t, 3),
                    end_s=round(p_end, 3),
                    text=p,
                    scene_id=sc.id,
                    approximate=len(phrases) > 1,
                )
            )
            t = p_end
    return caps


def _srt_time(s: float) -> str:
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def to_srt(captions: list[Caption]) -> str:
    blocks = []
    for c in captions:
        blocks.append(f"{c.index}\n{_srt_time(c.start_s)} --> {_srt_time(c.end_s)}\n{c.text}\n")
    return "\n".join(blocks)


def to_script(sb: Storyboard, tl: Timeline) -> str:
    lines = [
        f"{sb.branding.product_name or 'Demo'} — narration script",
        f"Aspect {sb.output.aspect_ratio} · requested {tl.requested_s:.0f}s · computed {tl.computed_s:.1f}s",
        "",
    ]
    for ts, sc in zip(tl.scenes, sb.scenes, strict=True):
        start = ts.start_frame / tl.fps
        end = ts.end_frame / tl.fps
        lines.append(f"Scene {ts.index + 1} [{sc.role}] {start:05.2f}s – {end:05.2f}s")
        if sc.headline:
            lines.append(f"  On screen: {sc.headline}")
        if sc.subline:
            lines.append(f"  Subline:   {sc.subline}")
        lines.append(f"  Narration: {sc.narration.strip() or '(none)'}")
        lines.append("")
    return "\n".join(lines)
