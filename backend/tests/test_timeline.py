"""Timeline resolution: measured speech is never truncated; one manifest drives everything."""

from __future__ import annotations

import math
import random

from demojo.storyboard import Scene, Storyboard
from demojo.timeline import DISSOLVE_FRAMES, MIN_SCENE_FRAMES, TAIL_FRAMES, resolve, to_srt


def _board(scenes, target=30, narration="tts"):
    return Storyboard(project_id="prj_t", scenes=scenes, output={"target_duration_s": target}, narration={"mode": narration})


def _random_scene(rng: random.Random, i: int) -> Scene:
    kind = rng.choice(["image", "clip", "title_card"])
    words = " ".join(["word"] * rng.randint(0, 40))
    common = dict(role="feature", planned_duration_s=round(rng.uniform(1.5, 8), 2), narration=words,
                  transition_in=rng.choice(["cut", "dissolve"]) if i else "cut")
    if kind == "clip":
        a = round(rng.uniform(0, 5), 2)
        return Scene(source_kind="clip", asset_id="ast_v", clip_in_s=a, clip_out_s=round(a + rng.uniform(0.5, 9), 2), **common)
    if kind == "image":
        return Scene(source_kind="image", asset_id="ast_i", **common)
    return Scene(source_kind="title_card", **common)


def test_measured_speech_is_never_truncated_property():
    rng = random.Random(1234)
    for _ in range(400):
        scenes = [_random_scene(rng, i) for i in range(rng.randint(1, 8))]
        board = _board(scenes)
        speech = {s.id: (round(rng.uniform(0.3, 14.0), 3), True, "k") for s in scenes if s.narration.strip()}
        tl = resolve(board, speech)
        prev_speech_end = -1
        for ts, sc in zip(tl.scenes, scenes, strict=True):
            assert ts.n_frames >= MIN_SCENE_FRAMES
            if ts.speech is not None:
                dur = speech[sc.id][0]
                # speech fully inside the scene with a tail of breathing room
                assert ts.speech.start_frame >= ts.start_frame + ts.overlap_frames
                assert ts.speech.end_frame == ts.speech.start_frame + math.ceil(dur * 30)
                assert ts.speech.end_frame <= ts.end_frame - TAIL_FRAMES
                # never overlaps the previous scene's narration
                assert ts.speech.start_frame >= prev_speech_end
                prev_speech_end = ts.speech.end_frame
            if ts.clip_frames is not None:
                assert ts.clip_frames + ts.hold_frames == ts.n_frames
        overlaps = sum(t.overlap_frames for t in tl.scenes)
        assert tl.total_frames == sum(t.n_frames for t in tl.scenes) - overlaps
        assert tl.total_frames == tl.scenes[-1].end_frame
        for c in tl.captions:
            assert 0 <= c.start_s < c.end_s <= tl.total_frames / 30 + 1e-6


def test_short_clip_holds_final_frame_instead_of_looping():
    sc = Scene(role="step", source_kind="clip", asset_id="ast_v", clip_in_s=2.0, clip_out_s=3.0, planned_duration_s=1.5,
               narration="This narration is clearly longer than one second of recorded footage.")
    tl = resolve(_board([sc]), {sc.id: (4.2, True, "k")})
    ts = tl.scenes[0]
    assert ts.clip_frames == 30
    assert ts.hold_frames == ts.n_frames - 30 > 0


def test_dissolve_overlap_and_runtime_flags():
    a = Scene(role="hook", source_kind="title_card", planned_duration_s=3)
    b = Scene(role="cta", source_kind="title_card", planned_duration_s=3, transition_in="dissolve")
    tl = resolve(_board([a, b], target=20, narration="none"))
    assert tl.scenes[1].overlap_frames == DISSOLVE_FRAMES
    assert tl.total_frames == 90 + 90 - DISSOLVE_FRAMES
    assert not tl.over_target
    long = Scene(role="feature", source_kind="title_card", planned_duration_s=3, narration="w " * 200)
    tl2 = resolve(_board([long], target=20), {long.id: (40.0, True, "k")})
    assert tl2.over_target and tl2.computed_s > 40


def test_estimates_are_labelled_and_srt_is_well_formed():
    sc = Scene(role="feature", source_kind="title_card", planned_duration_s=3,
               narration="First sentence here. Second sentence, with a clause, follows. Third one!")
    tl = resolve(_board([sc]))
    assert tl.all_speech_measured is False and tl.scenes[0].speech.measured is False
    srt = to_srt(tl.captions)
    blocks = [b for b in srt.strip().split("\n\n") if b]
    assert len(blocks) == len(tl.captions) >= 2
    for i, b in enumerate(blocks, 1):
        lines = b.split("\n")
        assert lines[0] == str(i)
        assert " --> " in lines[1] and lines[1][2] == ":" and "," in lines[1]
    assert all(c.approximate for c in tl.captions)
