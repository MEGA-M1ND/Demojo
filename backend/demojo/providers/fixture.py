"""Deterministic fixture provider (explicit DEMOJO_PROVIDER_MODE=fixture only).

No network calls and no AI. Storyboards are templates assembled strictly from
the user's own text and asset order; narration uses the local espeak-ng voice.
Every output is labelled "fixture" so it can never be mistaken for AI output.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from ..ai_schemas import (
    FrameNote,
    ImageAnalysis,
    PlannedClaim,
    PlannedPoint,
    PlannedScene,
    PlannedSceneOnly,
    PlannedStory,
    Segment,
    VideoAnalysis,
)
from ..config import Settings
from ..errors import AppError

FIXTURE_NOTE = "Fixture mode: deterministic template built from your own text — not AI-generated."


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p.strip() for p in parts if p.strip()]


def _clip_words(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars + 1].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut


def _clauses(text: str) -> list[str]:
    """Split workflow notes into ordered steps (the user's own words)."""
    out: list[str] = []
    for sent in _sentences(text):
        for part in re.split(r",\s*(?:and\s+|then\s+)?|;\s*|\s+then\s+", sent.rstrip(".!?")):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _headline(text: str, limit: int = 60) -> str:
    """First clause of a sentence, clipped at a word boundary, without trailing punctuation."""
    text = " ".join(text.split()).rstrip(".!?")
    for sep in (", ", " — ", "; ", ": "):
        head = text.split(sep)[0]
        if 12 <= len(head) < len(text):
            text = head
            break
    return _clip_words(text, limit).rstrip(",;:")


def _as_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return text if text[-1] in ".!?" else text + "."


class FixtureProvider:
    name = "fixture"
    vision_model = "fixture"
    story_model = "fixture"
    tts_model = "espeak-ng"
    tts_voice = "en-us"
    tts_format = "wav"

    def __init__(self, settings: Settings):
        self.s = settings

    # -- analysis ---------------------------------------------------------

    def analyze_image(self, ledger, asset: dict, messages: list[dict]) -> ImageAnalysis:
        kind = "physical_product" if asset.get("classification") == "photo" else "software_screenshot"
        return ImageAnalysis(
            kind=kind,
            summary=f"Fixture analysis (no AI call) of the user-labelled asset '{asset.get('label', '')}'.",
            visible_elements=[],
            readable_text=[],
            focus_areas=[],
            suggested_role="showcase" if kind == "physical_product" else "feature",
            uncertainty="Fixture mode does not inspect image content.",
        )

    def analyze_video(self, ledger, asset: dict, frames: list[dict], messages: list[dict], check) -> VideoAnalysis:
        n = len(frames)
        k = min(3, max(1, n // 2))
        bounds = [round(i * n / k) for i in range(k + 1)]
        segs = [
            Segment(start_frame_index=bounds[i], end_frame_index=max(bounds[i], bounds[i + 1] - 1),
                    label=f"Part {i + 1}", description="Fixture segment (evenly split; no AI call).")
            for i in range(k)
        ]
        return VideoAnalysis(
            summary=f"Fixture analysis (no AI call) of {n} sampled frames.",
            frames=[FrameNote(frame_index=f["index"], description="(fixture)", visible_text=[]) for f in frames],
            segments=segs,
            uncertainty="Fixture mode does not inspect frames.",
        )

    # -- planning ---------------------------------------------------------

    def plan_story(self, ledger, ctx, messages: list[dict], check) -> PlannedStory:
        """Template storyboard sized to the target duration using only the user's own sentences."""
        d = ctx.details
        points = [(p.strip(), f"selling_point_{i + 1}") for i, p in enumerate(d.selling_points) if p.strip()]
        desc = _sentences(d.description)
        clauses = _clauses(d.workflow_notes)
        filler = [(s, "description") for s in desc[1:]]
        target = float(d.target_duration_s)

        def claim(text: str, ref: str) -> list[PlannedClaim]:
            return [PlannedClaim(text=_clip_words(text, 290), basis="user_detail", source_ref=ref)] if text else []

        def need(text: str) -> float:  # seconds a line needs on screen (speech + breathing room)
            return len(text.split()) / 2.6 + 0.3 + 0.8 if text else 0.0

        def take(seconds: float, *pools: list[tuple[str, str]]) -> tuple[str, str]:
            for pool in pools:
                for i, (text, _ref) in enumerate(pool):
                    if need(text) <= seconds:
                        return pool.pop(i)
            return "", ""

        hook_line = desc[0] if desc else d.product_name
        hook_head = _headline(hook_line, 80) if desc else ""  # empty -> renderer shows the logo or product name
        cta_text = d.cta_text or f"Try {d.product_name}"
        cta_line = _as_sentence(_clip_words(f"{cta_text}. {d.website_text}".strip(" ."), 200))
        hook_s = max(3.0, need(hook_line))
        cta_s = max(3.0, need(cta_line))
        remaining = target - hook_s - cta_s

        image_refs = [r for r in ctx.ordered_refs if ctx.assets_by_ref[r]["kind"] == "image"][:5]
        video_ref = next((r for r in ctx.ordered_refs if ctx.assets_by_ref[r]["kind"] == "video"), None)
        segments: list[dict] = []
        if video_ref and ctx.video is not None:
            for seg in ctx.video["segments_s"][:3]:
                length = seg["end_s"] - seg["start_s"]
                if segments and remaining - length < 4.0 * len(image_refs):
                    break
                segments.append(seg)
                remaining -= length
        per_image = max(2.5, min(8.0, remaining / len(image_refs))) if image_refs else 0.0

        planned: dict[str, list[PlannedScene]] = {}
        motions = ["gentle_push_in", "pan_right", "gentle_push_in", "pan_left"]
        for seg in segments:
            length = seg["end_s"] - seg["start_s"]
            # Clips only get the user's own workflow steps (in order), never unrelated selling points.
            text = ""
            while clauses and need(_as_sentence(f"{text}, {clauses[0]}" if text else clauses[0])) <= length:
                text = f"{text}, {clauses.pop(0)}" if text else clauses.pop(0)
            text = _as_sentence(text[:1].upper() + text[1:]) if text else ""
            src = "workflow_notes"
            planned.setdefault(video_ref, []).append(PlannedScene(  # type: ignore[arg-type]
                role="step", source="clip", asset_ref=video_ref, clip_start_s=seg["start_s"], clip_end_s=seg["end_s"],
                duration_s=round(length, 2), headline=_headline(text), narration=_clip_words(text, 220),
                selection_reason=f"Recording segment {seg['label']} (fixture split, in source order).",
                claims=claim(text, src), focal_point=PlannedPoint(x=0.5, y=0.5), motion="static", transition_in="dissolve",
            ))
        role = "showcase" if d.product_type == "physical" else "feature"
        for n, ref in enumerate(image_refs):
            a = ctx.assets_by_ref[ref]
            # Selling point i goes with image i (upload order); narrate it only if it fits the time.
            text, src = points.pop(0) if points else take(per_image, filler)
            spoken = text if need(text) <= per_image else ""
            planned[ref] = [PlannedScene(
                role=role, source="image", asset_ref=ref, duration_s=round(per_image, 2),
                headline=_headline(text) or _clip_words(a["label"], 60), narration=_as_sentence(_clip_words(spoken, 220)),
                selection_reason=f"User-provided {a['classification']} '{a['label']}' in upload order.",
                claims=claim(spoken, src), focal_point=PlannedPoint(x=0.5, y=0.45),
                motion=motions[n % len(motions)], transition_in="dissolve",
            )]
        body = [sc for ref in ctx.ordered_refs for sc in planned.get(ref, [])]
        hook = PlannedScene(
            role="hook", source="title_card", asset_ref=None, duration_s=round(hook_s, 2),
            headline=_clip_words(hook_head, 80), subline=_clip_words(d.audience, 90),
            narration=_clip_words(hook_line, 220), selection_reason="Opening title card with the product name.",
            claims=claim(hook_line, "description"), focal_point=PlannedPoint(x=0.5, y=0.5),
            motion="gentle_push_in", transition_in="cut",
        )
        cta = PlannedScene(
            role="cta", source="title_card", asset_ref=None, duration_s=round(cta_s, 2), headline="",
            subline="", narration=cta_line, selection_reason="Closing call to action.", claims=[],
            focal_point=PlannedPoint(x=0.5, y=0.5), motion="static", transition_in="dissolve",
        )
        return PlannedStory(title=f"{d.product_name} demo (fixture)", warnings=[FIXTURE_NOTE], scenes=[hook, *body, cta])

    def regenerate_scene(self, ledger, ctx, scene_index: int, messages: list[dict], check) -> PlannedSceneOnly:
        story = self.plan_story(ledger, ctx, messages, check)
        cur = ctx.current_scene
        pick = next((s for s in story.scenes if s.asset_ref == cur.get("asset_ref") and s.source == cur.get("source")), None)
        pick = pick or story.scenes[min(scene_index, len(story.scenes) - 1)]
        return PlannedSceneOnly(scene=pick, note=FIXTURE_NOTE)

    # -- speech -----------------------------------------------------------

    def synthesize(self, ledger, text: str) -> tuple[bytes, str, str | None]:
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not exe:
            raise AppError("fixture_voice_unavailable", "Fixture narration needs espeak-ng installed (apt-get install espeak-ng).")
        # Text goes through stdin, never argv, so it can't be parsed as options.
        proc = subprocess.run([exe, "-v", "en-us", "-s", "160", "--stdin", "--stdout"], input=text.encode("utf-8"),
                              capture_output=True, timeout=60)
        if proc.returncode != 0 or len(proc.stdout) < 1000:
            raise AppError("narration_failed", "Fixture voice (espeak-ng) failed to produce audio.")
        return proc.stdout, "wav", None
