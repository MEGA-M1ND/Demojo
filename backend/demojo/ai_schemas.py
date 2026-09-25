"""Schemas for provider output.

Each has a strict JSON Schema (sent as response_format when supported) and a
Pydantic model used to validate the reply locally regardless. Provider output
is always treated as untrusted data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "2026-09-25.1"


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _nullable(schema: dict) -> dict:
    s = dict(schema)
    t = s.get("type")
    s["type"] = [t, "null"] if isinstance(t, str) else t
    return s


def _enum(values: list[str], desc: str) -> dict:
    return {"type": "string", "enum": values, "description": desc}


RECT = _obj({
    "x": _num("left edge, 0..1 of image width"),
    "y": _num("top edge, 0..1 of image height"),
    "w": _num("width, 0..1"),
    "h": _num("height, 0..1"),
})

ROLES = ["hook", "intro", "feature", "step", "showcase", "cta"]


# ---------------------------------------------------------------------------
# Image analysis
# ---------------------------------------------------------------------------

IMAGE_ANALYSIS_SCHEMA = _obj({
    "kind": _enum(["software_screenshot", "physical_product", "other"], "what the image shows"),
    "summary": _str("one or two sentences describing only what is visible"),
    "visible_elements": {"type": "array", "items": {"type": "string"}, "description": "UI elements or product parts that are clearly visible (max 12)"},
    "readable_text": {"type": "array", "items": {"type": "string"}, "description": "text you can read with confidence (max 12); omit anything uncertain"},
    "focus_areas": {
        "type": "array",
        "description": "up to 5 regions worth highlighting, normalised coordinates",
        "items": _obj({"label": _str("what the region contains"), "x": _num("0..1"), "y": _num("0..1"), "w": _num("0..1"), "h": _num("0..1")}),
    },
    "suggested_role": _enum([*ROLES, "none"], "best scene role for this asset"),
    "uncertainty": _str("what you are unsure about; empty string if nothing"),
})


class FocusArea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=120)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class ImageAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["software_screenshot", "physical_product", "other"]
    summary: str = Field(max_length=600)
    visible_elements: list[str] = Field(max_length=20)
    readable_text: list[str] = Field(max_length=20)
    focus_areas: list[FocusArea] = Field(max_length=8)
    suggested_role: Literal["hook", "intro", "feature", "step", "showcase", "cta", "none"]
    uncertainty: str = Field(max_length=600)


# ---------------------------------------------------------------------------
# Recording analysis (sampled frames)
# ---------------------------------------------------------------------------

VIDEO_ANALYSIS_SCHEMA = _obj({
    "summary": _str("what the sampled frames show overall; say it is based on sampled frames"),
    "frames": {
        "type": "array",
        "items": _obj({
            "frame_index": {"type": "integer", "description": "index of the frame as labelled"},
            "description": _str("what is visible in this frame"),
            "visible_text": {"type": "array", "items": {"type": "string"}},
        }),
    },
    "segments": {
        "type": "array",
        "description": "ordered workflow steps visible across consecutive frames",
        "items": _obj({
            "start_frame_index": {"type": "integer"},
            "end_frame_index": {"type": "integer"},
            "label": _str("short name for the step"),
            "description": _str("what visibly happens between these frames"),
        }),
    },
    "uncertainty": _str("what cannot be determined from sparse frames"),
})


class FrameNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_index: int = Field(ge=0)
    description: str = Field(max_length=500)
    visible_text: list[str] = Field(max_length=20)


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_frame_index: int = Field(ge=0)
    end_frame_index: int = Field(ge=0)
    label: str = Field(max_length=120)
    description: str = Field(max_length=500)


class VideoAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1000)
    frames: list[FrameNote] = Field(max_length=30)
    segments: list[Segment] = Field(max_length=12)
    uncertainty: str = Field(max_length=800)


# ---------------------------------------------------------------------------
# Storyboard planning
# ---------------------------------------------------------------------------

CLAIM = _obj({
    "text": _str("a factual statement made in the narration or on-screen text"),
    "basis": _enum(["user_detail", "visible_asset", "inferred"], "user_detail = stated in USER DETAILS; visible_asset = visible in an asset; inferred = anything else"),
    "source_ref": _nullable(_str("which user field (e.g. selling_point_2) or asset ref (e.g. A2, V1)")),
})

SCENE = _obj({
    "role": _enum(ROLES, "scene role"),
    "source": _enum(["image", "clip", "title_card"], "image = still asset, clip = segment of the recording, title_card = text on brand background"),
    "asset_ref": _nullable(_str("asset ref such as A1 or V1; null for title_card")),
    "clip_start_s": _nullable(_num("clip start in seconds (clip scenes only)")),
    "clip_end_s": _nullable(_num("clip end in seconds (clip scenes only)")),
    "duration_s": _num("planned scene duration in seconds (1.5..12)"),
    "headline": _str("short on-screen headline, max 60 characters"),
    "subline": _str("optional supporting line, max 90 characters, may be empty"),
    "narration": _str("spoken narration for this scene"),
    "selection_reason": _str("why this asset/segment was chosen, one sentence"),
    "claims": {"type": "array", "items": CLAIM},
    "focus_area": _nullable(RECT | {"description": "optional crop of the asset to show (useful for portrait output)"}),
    "highlight": _nullable(RECT | {"description": "optional region to outline; must be visible in the asset"}),
    "focal_point": _obj({"x": _num("0..1"), "y": _num("0..1")}),
    "motion": _enum(["static", "gentle_push_in", "pan_left", "pan_right", "focus_zoom"], "camera motion preset"),
    "transition_in": _enum(["cut", "dissolve"], "transition into this scene"),
})

STORY_SCHEMA = _obj({
    "title": _str("working title for the video"),
    "warnings": {"type": "array", "items": {"type": "string"}, "description": "limitations the user should know about"},
    "scenes": {"type": "array", "items": SCENE},
})

SCENE_ONLY_SCHEMA = _obj({"scene": SCENE, "note": _str("what changed and why, one sentence")})


class PlannedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300)
    basis: Literal["user_detail", "visible_asset", "inferred"]
    source_ref: str | None = Field(default=None, max_length=80)


class PlannedRect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    w: float
    h: float


class PlannedPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class PlannedScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["hook", "intro", "feature", "step", "showcase", "cta"]
    source: Literal["image", "clip", "title_card"]
    asset_ref: str | None = None
    clip_start_s: float | None = None
    clip_end_s: float | None = None
    duration_s: float
    headline: str = ""
    subline: str = ""
    narration: str = ""
    selection_reason: str = ""
    claims: list[PlannedClaim] = Field(default_factory=list, max_length=10)
    focus_area: PlannedRect | None = None
    highlight: PlannedRect | None = None
    focal_point: PlannedPoint
    motion: Literal["static", "gentle_push_in", "pan_left", "pan_right", "focus_zoom"]
    transition_in: Literal["cut", "dissolve"]


class PlannedStory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=120)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    scenes: list[PlannedScene] = Field(min_length=1, max_length=8)


class PlannedSceneOnly(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: PlannedScene
    note: str = Field(default="", max_length=400)
