"""Storyboard schema + semantic validation: provider output is untrusted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from demojo.ai_schemas import PlannedPoint, PlannedRect, PlannedScene, PlannedStory
from demojo.errors import AppError
from demojo.projects import ProjectDetails, get_storyboard, save_storyboard
from demojo.providers.openrouter import OpenRouterClient, extract_json
from demojo.storyboard import (
    AssetInfo,
    NormPoint,
    NormRect,
    Scene,
    Storyboard,
    export_blockers,
    validate_against_assets,
)

ASSETS = {
    "ast_img": AssetInfo("ast_img", "image", 1440, 900),
    "ast_vid": AssetInfo("ast_vid", "video", 1440, 900, 14.0),
    "ast_logo": AssetInfo("ast_logo", "logo", 500, 140),
}


def sb(*scenes: Scene, **kw) -> Storyboard:
    return Storyboard(project_id="prj_test", scenes=list(scenes), **kw)


def img_scene(**kw) -> Scene:
    return Scene(role="feature", source_kind="image", asset_id=kw.pop("asset_id", "ast_img"), planned_duration_s=4, **kw)


def clip_scene(a: float, b: float, **kw) -> Scene:
    return Scene(role="step", source_kind="clip", asset_id=kw.pop("asset_id", "ast_vid"), clip_in_s=a, clip_out_s=b,
                 planned_duration_s=max(1.5, b - a), **kw)


def test_valid_storyboard_passes():
    rep = validate_against_assets(sb(img_scene(), clip_scene(1.0, 6.0)), ASSETS)
    assert rep.ok, rep.issues


def test_foreign_asset_id_rejected():
    rep = validate_against_assets(sb(img_scene(asset_id="ast_from_other_project")), ASSETS)
    assert [i.code for i in rep.errors] == ["foreign_asset"]


def test_logo_cannot_be_scene_media_and_foreign_logo_rejected():
    rep = validate_against_assets(sb(img_scene(asset_id="ast_logo"), branding={"logo_asset_id": "ast_nope"}), ASSETS)
    codes = {i.code for i in rep.errors}
    assert {"logo_as_scene", "foreign_logo"} <= codes


def test_out_of_bounds_clip_times_rejected():
    rep = validate_against_assets(sb(clip_scene(10.0, 15.5)), ASSETS)
    assert [i.code for i in rep.errors] == ["clip_out_of_bounds"]
    with pytest.raises(ValidationError):
        clip_scene(5.0, 4.0)  # out before in
    with pytest.raises(ValidationError):
        clip_scene(-1.0, 3.0)  # negative
    with pytest.raises(ValidationError):
        clip_scene(2.0, 2.2)  # shorter than the 0.5 s minimum


def test_clip_must_reference_a_recording():
    rep = validate_against_assets(sb(clip_scene(0, 3, asset_id="ast_img")), ASSETS)
    assert "clip_needs_video" in {i.code for i in rep.errors}


def test_invalid_focal_coordinates_rejected():
    with pytest.raises(ValidationError):
        NormPoint(x=1.2, y=0.5)
    with pytest.raises(ValidationError):
        NormPoint(x=0.5, y=-0.01)
    with pytest.raises(ValidationError):
        NormRect(x=0.8, y=0.1, w=0.4, h=0.2)  # extends past the right edge
    with pytest.raises(ValidationError):
        NormRect(x=0.1, y=0.1, w=0.0, h=0.2)  # degenerate


def test_unsupported_schema_output_rejected():
    good = sb(img_scene()).model_dump()
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"schema_version": "demojo.storyboard/99"})
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"ffmpeg_filter": "drawtext=text='x'"})  # unknown field
    bad_scene = dict(good["scenes"][0], motion="$(rm -rf /)")
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"scenes": [bad_scene]})
    bad_scene = dict(good["scenes"][0], transition_in="wipe_star")
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"scenes": [bad_scene]})
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"scenes": []})
    with pytest.raises(ValidationError):
        Storyboard.model_validate(good | {"scenes": [good["scenes"][0]] * 9})


def test_title_cards_cannot_reference_assets():
    with pytest.raises(ValidationError):
        Scene(role="hook", source_kind="title_card", asset_id="ast_img", planned_duration_s=3)


def test_unrenderable_text_is_reported():
    rep = validate_against_assets(sb(img_scene(headline="Launch day 🚀")), ASSETS)
    assert "unrenderable_text" in {i.code for i in rep.errors}


def test_inferred_claims_require_confirmation_before_export():
    s = img_scene(narration="Cuts invoicing time by 80%.",
                  claims=[{"text": "Cuts invoicing time by 80%.", "basis": "inferred"}])
    assert s.claims[0].status == "needs_confirmation"
    board = sb(s)
    assert [b.code for b in export_blockers(board)] == ["claim_unconfirmed"]
    s.claims[0].status = "confirmed"
    assert export_blockers(board) == []


def test_extract_json_handles_fences_and_rejects_garbage():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")
    probs = OpenRouterClient._problems("{not json", PlannedStory, None)
    assert isinstance(probs, list) and "not valid JSON" in probs[0]
    probs = OpenRouterClient._problems('{"title": "x", "scenes": [{"role": "villain"}]}', PlannedStory, None)
    assert isinstance(probs, list) and probs


def _ctx(make_project):
    from demojo.planner import build_context

    p = make_project("software", images=True, recording=True)
    d = ProjectDetails.model_validate(p["details"])
    vid = next(a for a in p["assets"] if a["kind"] == "video")
    analyses = {vid["id"]: {"segments_s": [{"start_s": 0.0, "end_s": 7.0, "label": "a", "description": ""}], "sampled_frames": 5}}
    return build_context(p["id"], d, analyses)


def _planned(**kw) -> PlannedScene:
    base = dict(role="feature", source="image", asset_ref="A1", duration_s=4, headline="h", narration="n",
                focal_point=PlannedPoint(x=0.5, y=0.5), motion="static", transition_in="cut")
    base.update(kw)
    return PlannedScene(**base)


def test_planner_semantic_check_catches_untrusted_output(make_project):
    from demojo.planner import story_check

    ctx = _ctx(make_project)
    check = story_check(ctx)
    story = PlannedStory(title="t", scenes=[
        _planned(asset_ref="A9"),  # foreign ref
        _planned(source="clip", asset_ref="V1", clip_start_s=8.0, clip_end_s=20.0),  # past end of 14 s recording
        _planned(highlight=PlannedRect(x=0.9, y=0.1, w=0.3, h=0.3)),  # outside the image
        _planned(focal_point=PlannedPoint(x=2, y=0.5)),
        _planned(source="clip", asset_ref="V1", clip_start_s=1.0, clip_end_s=3.0),  # out of source order
        _planned(source="title_card", asset_ref="A1"),
        _planned(headline="x" * 200),
    ])
    errs = check(story)
    joined = " | ".join(errs)
    assert "A9" in joined
    assert "clip times" in joined
    assert "highlight" in joined
    assert "focal_point" in joined
    assert "chronological" in joined
    assert "title_card must have asset_ref null" in joined
    assert "headline is too long" in joined


def test_planned_to_scene_guards_unsupported_numbers(make_project):
    from demojo.planner import planned_to_scene

    ctx = _ctx(make_project)
    sc = planned_to_scene(_planned(narration="Tallyfox is the fastest way to bill, trusted by 10,000 teams."), ctx, "openrouter")
    flagged = [c for c in sc.claims if c.status == "needs_confirmation"]
    assert flagged, "unsupported superlatives/numbers must be flagged"
    # Numbers that appear in the user's own text are fine.
    ok = planned_to_scene(_planned(narration="Send an invoice from one screen."), ctx, "openrouter")
    assert not [c for c in ok.claims if c.status == "needs_confirmation"]


def test_save_rejects_foreign_asset_and_stale_revision(make_project):
    a = make_project("software")
    b = make_project("physical")
    foreign = b["assets"][0]["id"]
    board = sb(img_scene(asset_id=a["assets"][0]["id"])).model_dump()
    saved, _ = save_storyboard(a["id"], board, base_revision=None, source="fixture")
    assert saved.revision == 1
    bad = saved.model_dump()
    bad["scenes"][0]["asset_id"] = foreign
    with pytest.raises(AppError) as e:
        save_storyboard(a["id"], bad, base_revision=1)
    assert e.value.code == "invalid_storyboard"
    edit = saved.model_dump()
    edit["scenes"][0]["headline"] = "v2"
    save_storyboard(a["id"], edit, base_revision=1)
    stale = saved.model_dump()
    stale["scenes"][0]["headline"] = "stale write"
    with pytest.raises(AppError) as e:
        save_storyboard(a["id"], stale, base_revision=1)
    assert e.value.code == "stale_revision"
    assert get_storyboard(a["id"]).scenes[0].headline == "v2"


def test_recording_segments_may_share_a_boundary_frame_but_not_overlap_in_time():
    from demojo.ai_schemas import Segment, VideoAnalysis
    from demojo.analysis import segments_to_seconds, video_check

    frames = [{"index": i, "t": t} for i, t in enumerate([0.5, 3.0, 6.0, 9.0, 12.0])]
    seg = lambda a, b: Segment(start_frame_index=a, end_frame_index=b, label="s", description="d")  # noqa: E731
    touching = VideoAnalysis(summary="", frames=[], segments=[seg(0, 2), seg(2, 4)], uncertainty="")
    assert video_check(5)(touching) == []
    backwards = VideoAnalysis(summary="", frames=[], segments=[seg(0, 3), seg(1, 4)], uncertainty="")
    assert video_check(5)(backwards)
    spans = segments_to_seconds(touching.model_dump(), frames, 14.0)
    assert spans[0]["end_s"] <= spans[1]["start_s"]
