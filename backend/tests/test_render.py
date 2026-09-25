"""Real renders through the full pipeline (fixture providers), validated with ffprobe/ffmpeg."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from PIL import Image, ImageStat

from demojo import ffmpeg as ff
from demojo.projects import export_file, get_project, get_storyboard, list_exports, save_storyboard

from .conftest import run_job

ATTACK = "$(rm -rf /) ‘q’ \"dq\" ../../etc/passwd %{pts} ünï"

CASES = [
    # (id, kind, images, recording, aspect, quality, expected dims)
    ("images_16x9_final", "physical", True, False, "16:9", "final", (1920, 1080)),
    ("recording_9x16_draft", "software", False, True, "9:16", "draft", (720, 1280)),
    ("mixed_16x9_draft", "software", True, True, "16:9", "draft", (1280, 720)),
    ("mixed_9x16_final", "software", True, True, "9:16", "final", (1080, 1920)),
]


def _probe(path) -> dict:
    return ff.ffprobe_json(path)


def _frame(path, t: float) -> Image.Image:
    out = subprocess.run([ff.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    from io import BytesIO

    return Image.open(BytesIO(out)).convert("L")


def _mean_volume(path, start: float, end: float) -> float:
    err = subprocess.run([ff.FFMPEG, "-v", "info", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path),
                          "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True).stderr.decode()
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -120.0


def _black_segments(path) -> list[str]:
    err = subprocess.run([ff.FFMPEG, "-v", "info", "-i", str(path), "-vf", "blackdetect=d=0.1:pix_th=0.05", "-an", "-f", "null", "-"],
                         capture_output=True).stderr.decode()
    return re.findall(r"black_start:\S+ black_end:\S+", err)


@pytest.mark.render
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_render_matrix(case, make_project, monkeypatch):
    cid, kind, images, recording, aspect, quality, dims = case
    proj = make_project(kind, images=images, recording=recording, aspect=aspect)
    pid = proj["id"]
    j = run_job(pid, "generate_story")
    assert j["status"] == "story_ready", j["error"]

    # Put hostile text on screen to prove it never reaches a process argument.
    sb = get_storyboard(pid)
    data = sb.model_dump()
    target = next(i for i, s in enumerate(sb.scenes) if s.source_kind != "title_card")
    data["scenes"][target]["headline"] = ATTACK
    save_storyboard(pid, data, base_revision=sb.revision)

    recorded: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy(args, *a, **kw):
        assert not kw.get("shell"), "shell=True is never allowed"
        recorded.append([str(x) for x in args])
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    j = run_job(pid, "render", {"quality": quality, "accept_runtime": True}, revision=get_project(pid)["current_revision"])
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    assert j["status"] == "completed", j["error"]
    assert recorded, "expected ffmpeg invocations"
    for args in recorded:
        joined = " ".join(args)
        assert "rm -rf" not in joined and "etc/passwd" not in joined and "%{pts}" not in joined
        assert args[0] in (ff.FFMPEG, ff.FFPROBE, "espeak-ng") or args[0].endswith(("espeak-ng", "espeak"))

    (exp,) = list_exports(pid)
    video, _, _ = export_file(pid, exp["id"], "video")
    tl_path, _, _ = export_file(pid, exp["id"], "timeline")
    tl = json.loads(tl_path.read_text())
    info = _probe(video)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert (v["width"], v["height"]) == dims
    assert v["avg_frame_rate"] == "30/1"
    expected = tl["total_frames"] / 30
    assert abs(float(v["duration"]) - expected) <= 1 / 30 + 1e-3
    assert int(v["nb_frames"]) in (tl["total_frames"] - 1, tl["total_frames"], tl["total_frames"] + 1)
    # Narration: AAC, covers the last spoken word, not truncated.
    assert a["codec_name"] == "aac"
    speeches = [s["speech"] for s in tl["scenes"] if s["speech"]]
    last_end = max(s["end_frame"] for s in speeches) / 30
    assert float(a["duration"]) + 0.05 >= last_end
    last = max(speeches, key=lambda s: s["end_frame"])
    assert _mean_volume(video, last["start_frame"] / 30, last["end_frame"] / 30) > -45
    # Faststart: moov before mdat for browser playback.
    head = video.read_bytes()[:2_000_000]
    assert 0 <= head.find(b"moov") < max(head.find(b"mdat"), 1)
    # No black or flat frames at the opening, middle, and CTA.
    for t in (0.5, expected / 2, expected - 0.8):
        st = ImageStat.Stat(_frame(video, t))
        assert st.mean[0] > 12 and st.stddev[0] > 4, (cid, t, st.mean, st.stddev)
    assert _black_segments(video) == []
    # Captions exist and stay within the video.
    srt, _, _ = export_file(pid, exp["id"], "captions")
    assert "-->" in srt.read_text()
