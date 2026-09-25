"""Fast unit checks for layout helpers (no video encoding)."""

from __future__ import annotations

from PIL import Image, ImageDraw

from demojo.providers.fixture import _clauses
from demojo.render import Box, CameraPlan, fit_contain, logo_for_theme, make_layout, source_region, theme_for
from demojo.storyboard import NormRect, Scene, Storyboard


def _theme(style: str):
    sb = Storyboard(project_id="p", style=style, scenes=[Scene(role="hook", source_kind="title_card", planned_duration_s=3)])
    return theme_for(sb)


def _logo(fill) -> Image.Image:
    im = Image.new("RGBA", (300, 80), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((10, 10, 290, 70), fill=fill)
    return im


def test_white_logo_gets_dark_backing_on_light_theme_only():
    white = _logo((255, 255, 255, 255))
    assert logo_for_theme(white, _theme("guided_walkthrough"), 80).size != white.size
    assert logo_for_theme(white, _theme("clean_launch"), 80).size == white.size
    black = _logo((5, 5, 5, 255))
    assert logo_for_theme(black, _theme("clean_launch"), 80).size != black.size
    orange = _logo((234, 88, 12, 255))
    assert logo_for_theme(orange, _theme("guided_walkthrough"), 80).size == orange.size


def test_contain_never_stretches_and_focus_region_crops():
    box = Box(0, 0, 1000, 500)
    x, y, w, h = fit_contain(1600, 1000, box)
    assert abs(w / h - 1.6) < 0.01 and w <= 1000 and h <= 500
    sc = Scene(role="feature", source_kind="image", asset_id="a", planned_duration_s=3, focus_region=NormRect(x=0.1, y=0.2, w=0.5, h=0.3))
    x, y, w, h = source_region(sc, 1600, 1000, box)
    assert x < 0.1 and y < 0.2 and x + w > 0.6 and y + h > 0.5  # focus region plus a safety margin
    strip = Scene(role="feature", source_kind="image", asset_id="a", planned_duration_s=3,
                  focus_region=NormRect(x=0.18, y=0.22, w=0.77, h=0.1))
    x, y, w, h = source_region(strip, 1600, 1000, box)
    assert (w * 1600) / (h * 1000) <= 2 * 1.3 + 1e-6 and y <= 0.22 and y + h >= 0.32  # widened around the strip


def test_camera_window_stays_inside_the_frame():
    for motion in ("static", "gentle_push_in", "pan_left", "pan_right", "focus_zoom"):
        cam = CameraPlan(motion=motion, focal=(0.98, 0.02), target=(0.8, 0.0, 0.2, 0.1), zoom_to=2.0)
        for i in range(21):
            s, cx, cy = cam.window(i / 20)
            half = 0.5 / s
            assert s >= 1.0 and cx - half >= -1e-9 and cx + half <= 1 + 1e-9 and cy - half >= -1e-9 and cy + half <= 1 + 1e-9


def test_layouts_reserve_a_headline_band_outside_the_content():
    for W, H in ((1920, 1080), (1080, 1920), (1280, 720), (720, 1280)):
        lay = make_layout(W, H)
        c, h = lay.content, lay.headline
        assert c.x >= 0 and c.y >= 0 and c.x + c.w <= W and c.y + c.h <= H
        if W > H:
            assert h.y >= c.y + c.h  # headline band below the card
        else:
            assert h.y + h.h <= c.y + 1  # headline band above the card


def test_workflow_clauses_keep_order_and_words():
    assert _clauses("Open the dashboard, click New invoice, then click Send. Done; finally close it.") == [
        "Open the dashboard", "click New invoice", "click Send", "Done", "finally close it",
    ]
