"""Canonical, versioned storyboard schema shared by the API, planner, and renderer.

Everything here is declarative data. Provider output is parsed into these models
and then validated against the project's real assets before it is stored.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "demojo.storyboard/1"

AspectRatio = Literal["16:9", "9:16"]
TargetDuration = Literal[20, 30, 45, 60]
SceneRole = Literal["hook", "intro", "feature", "step", "showcase", "cta"]
SourceKind = Literal["image", "clip", "title_card"]
FitMode = Literal["contain", "cover"]
Motion = Literal["static", "gentle_push_in", "pan_left", "pan_right", "focus_zoom"]
Transition = Literal["cut", "dissolve"]
Style = Literal["clean_launch", "guided_walkthrough"]
ProductType = Literal["software", "physical"]
ClaimBasis = Literal["user_detail", "visible_asset", "inferred"]
ClaimStatus = Literal["ok", "needs_confirmation", "confirmed", "removed"]
Origin = Literal["openrouter", "fixture", "user"]

MOTIONS: tuple[str, ...] = ("static", "gentle_push_in", "pan_left", "pan_right", "focus_zoom")
TRANSITIONS: tuple[str, ...] = ("cut", "dissolve")

MAX_SCENES = 8
MIN_SCENE_S = 1.5
MAX_SCENE_S = 20.0
MAX_TOTAL_PLANNED_S = 90.0
MIN_CLIP_S = 0.5
FPS = 30

HEADLINE_MAX = 80
SUBLINE_MAX = 120
NARRATION_MAX = 420

SCENE_ID_RE = re.compile(r"^scn_[a-z0-9]{8}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

FINAL_DIMS = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
DRAFT_DIMS = {"16:9": (1280, 720), "9:16": (720, 1280)}


def new_scene_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "scn_" + "".join(secrets.choice(alphabet) for _ in range(8))


def new_claim_id() -> str:
    return "clm_" + secrets.token_hex(4)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormPoint(_Strict):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class NormRect(_Strict):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.02, le=1.0)
    h: float = Field(ge=0.02, le=1.0)

    @model_validator(mode="after")
    def _inside(self) -> NormRect:
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("rectangle extends outside the asset (x+w and y+h must be <= 1)")
        return self


class Claim(_Strict):
    id: str = Field(default_factory=new_claim_id, pattern=r"^clm_[a-z0-9]{4,16}$")
    text: str = Field(min_length=1, max_length=300)
    basis: ClaimBasis
    source_ref: str | None = Field(default=None, max_length=80)
    status: ClaimStatus = "ok"

    @model_validator(mode="after")
    def _inferred_needs_review(self) -> Claim:
        if self.basis == "inferred" and self.status == "ok":
            self.status = "needs_confirmation"
        return self


class Scene(_Strict):
    id: str = Field(default_factory=new_scene_id)
    role: SceneRole
    source_kind: SourceKind
    asset_id: str | None = None
    clip_in_s: float | None = Field(default=None, ge=0.0)
    clip_out_s: float | None = Field(default=None, ge=0.0)
    planned_duration_s: float = Field(ge=MIN_SCENE_S, le=MAX_SCENE_S)
    headline: str = Field(default="", max_length=HEADLINE_MAX)
    subline: str = Field(default="", max_length=SUBLINE_MAX)
    narration: str = Field(default="", max_length=NARRATION_MAX)
    selection_reason: str = Field(default="", max_length=300)
    claims: list[Claim] = Field(default_factory=list, max_length=10)
    fit_mode: FitMode = "contain"
    focus_region: NormRect | None = None
    focal_point: NormPoint = Field(default_factory=lambda: NormPoint(x=0.5, y=0.5))
    highlight: NormRect | None = None
    motion: Motion = "static"
    transition_in: Transition = "cut"
    origin: Origin = "user"

    @field_validator("id")
    @classmethod
    def _scene_id(cls, v: str) -> str:
        if not SCENE_ID_RE.match(v):
            raise ValueError("scene id must look like scn_xxxxxxxx")
        return v

    @model_validator(mode="after")
    def _source_consistency(self) -> Scene:
        if self.source_kind == "title_card":
            if self.asset_id is not None:
                raise ValueError("title_card scenes must not reference an asset")
            if self.clip_in_s is not None or self.clip_out_s is not None:
                raise ValueError("title_card scenes have no clip boundaries")
        else:
            if not self.asset_id:
                raise ValueError(f"{self.source_kind} scenes require an asset_id")
        if self.source_kind == "clip":
            if self.clip_in_s is None or self.clip_out_s is None:
                raise ValueError("clip scenes require clip_in_s and clip_out_s")
            if self.clip_out_s - self.clip_in_s < MIN_CLIP_S:
                raise ValueError(f"clip must be at least {MIN_CLIP_S}s long (clip_out_s > clip_in_s)")
        elif self.source_kind == "image":
            if self.clip_in_s is not None or self.clip_out_s is not None:
                raise ValueError("image scenes have no clip boundaries")
        return self


class OutputSpec(_Strict):
    aspect_ratio: AspectRatio = "16:9"
    fps: Literal[30] = 30
    target_duration_s: TargetDuration = 30

    @property
    def final_dims(self) -> tuple[int, int]:
        return FINAL_DIMS[self.aspect_ratio]

    @property
    def draft_dims(self) -> tuple[int, int]:
        return DRAFT_DIMS[self.aspect_ratio]


class Branding(_Strict):
    product_name: str = Field(default="", max_length=60)
    accent_color: str = "#5B5BD6"
    logo_asset_id: str | None = None
    cta_text: str = Field(default="", max_length=60)
    website_text: str = Field(default="", max_length=80)

    @field_validator("accent_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX_RE.match(v):
            raise ValueError("accent_color must be a #RRGGBB hex colour")
        return v.upper()


class NarrationSpec(_Strict):
    mode: Literal["tts", "none"] = "tts"
    model: str | None = None
    voice: str | None = None


class Storyboard(_Strict):
    schema_version: Literal["demojo.storyboard/1"] = SCHEMA_VERSION
    project_id: str
    revision: int = 0
    output: OutputSpec = Field(default_factory=OutputSpec)
    branding: Branding = Field(default_factory=Branding)
    style: Style = "clean_launch"
    product_type: ProductType = "software"
    narration: NarrationSpec = Field(default_factory=NarrationSpec)
    scenes: list[Scene] = Field(min_length=1, max_length=MAX_SCENES)
    warnings: list[str] = Field(default_factory=list, max_length=30)
    estimated_cost_usd: float | None = None
    cost_is_estimate: bool = True
    generated_by: Origin = "user"
    generation_models: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> Storyboard:
        ids = [s.id for s in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("scene ids must be unique")
        return self


# ---------------------------------------------------------------------------
# Semantic validation against project assets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetInfo:
    id: str
    kind: Literal["image", "video", "logo"]
    width: int
    height: int
    duration_s: float | None = None


@dataclass
class Issue:
    level: Literal["error", "warning"]
    code: str
    message: str
    scene_id: str | None = None

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message, "scene_id": self.scene_id}


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, code: str, message: str, scene_id: str | None = None) -> None:
        self.issues.append(Issue(level, code, message, scene_id))  # type: ignore[arg-type]


class StoryboardInvalid(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("; ".join(i.message for i in report.errors) or "invalid storyboard")


def validate_against_assets(sb: Storyboard, assets: dict[str, AssetInfo]) -> ValidationReport:
    """Check that the storyboard only references this project's assets, in bounds."""
    from .text_render import unsupported_chars  # local import: fonts load lazily

    rep = ValidationReport()
    total = 0.0
    for idx, sc in enumerate(sb.scenes):
        label = f"Scene {idx + 1}"
        if sc.asset_id is not None:
            info = assets.get(sc.asset_id)
            if info is None:
                rep.add("error", "foreign_asset", f"{label} references an asset that is not part of this project.", sc.id)
                continue
            if info.kind == "logo":
                rep.add("error", "logo_as_scene", f"{label} uses the logo as scene media.", sc.id)
            if sc.source_kind == "clip":
                if info.kind != "video":
                    rep.add("error", "clip_needs_video", f"{label} is a clip but its asset is not a recording.", sc.id)
                elif info.duration_s is not None and sc.clip_out_s is not None:
                    if sc.clip_out_s > info.duration_s + 1e-3:
                        rep.add(
                            "error",
                            "clip_out_of_bounds",
                            f"{label} clip ends at {sc.clip_out_s:.2f}s but the recording is {info.duration_s:.2f}s long.",
                            sc.id,
                        )
            elif sc.source_kind == "image" and info.kind != "image":
                rep.add("error", "image_needs_image", f"{label} is an image scene but its asset is a recording.", sc.id)
        total += scene_duration_hint(sc)
        for fld in ("headline", "subline", "narration"):
            bad = unsupported_chars(getattr(sc, fld))
            if bad:
                shown = " ".join(sorted(bad))[:40]
                rep.add("error", "unrenderable_text", f"{label} {fld} contains characters the renderer cannot draw: {shown}", sc.id)
        if sc.source_kind == "title_card" and sc.motion == "focus_zoom" and sc.highlight is None:
            rep.add("warning", "focus_without_highlight", f"{label} uses focus zoom without a highlight region.", sc.id)
    if total > MAX_TOTAL_PLANNED_S:
        rep.add("error", "runtime_too_long", f"Planned runtime {total:.1f}s exceeds the {MAX_TOTAL_PLANNED_S:.0f}s prototype limit.")
    logo = sb.branding.logo_asset_id
    if logo is not None and (logo not in assets or assets[logo].kind != "logo"):
        rep.add("error", "foreign_logo", "The logo does not belong to this project.")
    for fld in ("product_name", "cta_text", "website_text"):
        bad = unsupported_chars(getattr(sb.branding, fld))
        if bad:
            rep.add("error", "unrenderable_text", f"Branding {fld} contains unsupported characters: {' '.join(sorted(bad))[:40]}")
    return rep


def export_blockers(sb: Storyboard) -> list[Issue]:
    """Things that must be resolved by the user before an export may start."""
    out: list[Issue] = []
    for idx, sc in enumerate(sb.scenes):
        for c in sc.claims:
            if c.status == "needs_confirmation":
                out.append(
                    Issue("error", "claim_unconfirmed", f"Scene {idx + 1}: confirm or remove the claim “{c.text}”.", sc.id)
                )
    return out


def scene_duration_hint(sc: Scene) -> float:
    if sc.source_kind == "clip" and sc.clip_in_s is not None and sc.clip_out_s is not None:
        return sc.clip_out_s - sc.clip_in_s
    return sc.planned_duration_s
