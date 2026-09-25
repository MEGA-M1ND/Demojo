"""Deterministic renderer: storyboard + timeline + real assets -> MP4.

Composition happens in Python (Pillow) so draft and final renders share the
exact same logic; only the output size and encoder quality differ.

Per scene we render an intermediate H.264 segment (cached by content hash of
everything that affects its pixels). The final pass joins segments with cuts
or short dissolves, mixes narration at the timeline's frame offsets, and
encodes H.264/yuv420p + AAC with faststart. The result is validated with
ffprobe and a full decode pass before it is reported as finished.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from . import ffmpeg as ff
from .storyboard import FPS, Scene, Storyboard
from .text_render import TextFitError, draw_block, layout
from .timeline import Timeline, TimelineScene

RENDERER_VERSION = "demojo-render/9"
AUDIO_RATE = 48000
SAMPLES_PER_FRAME = AUDIO_RATE // FPS  # 1600

ProgressCb = Callable[[str, float | None], None]
CancelCheck = Callable[[], bool]


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetMedia:
    id: str
    kind: str  # image | video | logo
    path: Path  # orientation-normalised master (image) or original recording (video)
    sha256: str
    width: int  # display dimensions
    height: int
    duration_s: float | None = None
    has_alpha: bool = False


@dataclass
class Quality:
    name: str  # draft | final
    width: int
    height: int
    seg_crf: int
    seg_preset: str
    out_crf: int
    out_preset: str


def quality_for(sb: Storyboard, name: str) -> Quality:
    if name == "draft":
        w, h = sb.output.draft_dims
        return Quality("draft", w, h, 18, "veryfast", 25, "veryfast")
    w, h = sb.output.final_dims
    return Quality("final", w, h, 14, "veryfast", 19, "medium")


# ---------------------------------------------------------------------------
# Colours, theme, layout
# ---------------------------------------------------------------------------


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))  # type: ignore[return-value]


def _luma(c: tuple[int, int, int]) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


@dataclass(frozen=True)
class Theme:
    dark: bool
    bg_a: tuple[int, int, int]
    bg_b: tuple[int, int, int]
    glow: tuple[int, int, int]
    glow_alpha: float
    text: tuple[int, int, int]
    subtext: tuple[int, int, int]
    accent: tuple[int, int, int]
    on_accent: tuple[int, int, int]
    card_border: tuple[int, int, int, int]
    dim_alpha: int


def theme_for(sb: Storyboard) -> Theme:
    accent = hex_rgb(sb.branding.accent_color)
    on_accent = (255, 255, 255) if _luma(accent) < 150 else (15, 17, 24)
    if sb.style == "guided_walkthrough":
        return Theme(
            dark=False,
            bg_a=mix((246, 247, 249), accent, 0.04),
            bg_b=mix((232, 235, 240), accent, 0.07),
            glow=accent,
            glow_alpha=0.12,
            text=(15, 23, 42),
            subtext=(71, 85, 105),
            accent=accent,
            on_accent=on_accent,
            card_border=(15, 23, 42, 28),
            dim_alpha=70,
        )
    base = (8, 9, 13)
    return Theme(
        dark=True,
        bg_a=mix(accent, base, 0.84),
        bg_b=mix(accent, base, 0.95),
        glow=accent,
        glow_alpha=0.30,
        text=(250, 250, 252),
        subtext=(196, 200, 212),
        accent=accent,
        on_accent=on_accent,
        card_border=(255, 255, 255, 30),
        dim_alpha=95,
    )


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    def scaled(self, k: float) -> Box:
        return Box(self.x * k, self.y * k, self.w * k, self.h * k)


@dataclass(frozen=True)
class Layout:
    W: int
    H: int
    u: float  # unit scale relative to the 1080-short-side design grid
    portrait: bool
    content: Box
    headline: Box
    headline_max_lines: int
    headline_size: int
    headline_min: int
    logo: Box
    radius: int


def make_layout(W: int, H: int) -> Layout:
    portrait = H > W
    u = min(W, H) / 1080.0
    if not portrait:
        m = 96 * u
        band = 196 * u
        content = Box(m, 60 * u, W - 2 * m, H - band - 60 * u - 28 * u)
        headline = Box(m, H - band, W - 2 * m - 260 * u, band - 40 * u)
        logo = Box(W - m - 220 * u, H - band + 36 * u, 220 * u, 84 * u)
        return Layout(W, H, u, False, content, headline, 2, int(56 * u), int(34 * u), logo, int(20 * u))
    m = 64 * u
    headline = Box(m, 150 * u, W - 2 * m, 330 * u)
    content = Box(m, 510 * u, W - 2 * m, H - 510 * u - 230 * u)
    logo = Box((W - 300 * u) / 2, H - 170 * u, 300 * u, 90 * u)
    return Layout(W, H, u, True, content, headline, 3, int(66 * u), int(40 * u), logo, int(24 * u))


@lru_cache(maxsize=16)
def _background(theme: Theme, W: int, H: int) -> Image.Image:
    vert = Image.linear_gradient("L").resize((W, H), Image.BILINEAR)
    horiz = Image.linear_gradient("L").rotate(90).resize((W, H), Image.BILINEAR)
    grad = Image.blend(vert, ImageOps.invert(horiz), 0.45)
    img = Image.composite(Image.new("RGB", (W, H), theme.bg_b), Image.new("RGB", (W, H), theme.bg_a), grad)
    # Soft accent glows (radial masks never have hard edges).
    for cx, cy, r, a in ((0.1, 0.0, 1.1, theme.glow_alpha), (1.0, 1.0, 0.9, theme.glow_alpha * 0.45)):
        size = int(max(W, H) * r)
        mask = ImageOps.invert(Image.radial_gradient("L")).resize((size, size), Image.BICUBIC)
        mask = mask.point(lambda v, a=a: int((v / 255.0) ** 1.6 * 255 * a))
        layer = Image.new("RGB", (size, size), theme.glow)
        img.paste(layer, (int(cx * W - size / 2), int(cy * H - size / 2)), mask)
    # Gentle grain to prevent visible banding after 8-bit encoding.
    noise = Image.effect_noise((W, H), 18).convert("RGB")
    return Image.blend(img, noise, 0.025)


def logo_for_theme(logo_img: Image.Image, theme: Theme, height: int) -> Image.Image:
    """Resize-aware logo placement: add a backing pill when much of the logo would blend into the background."""
    lg = logo_img.convert("RGBA")
    alpha = lg.getchannel("A")
    solid = alpha.point(lambda a: 255 if a > 128 else 0)
    total = ImageStat.Stat(solid).sum[0] / 255
    if total == 0:
        return lg
    bg_luma = _luma(mix(theme.bg_a, theme.bg_b, 0.5))
    lum = lg.convert("L")
    # The failure case is a near-white logo on a light background (or near-black on dark).
    if bg_luma > 128:
        cut = max(200.0, bg_luma - 25)
        vanishing = lum.point(lambda v: 255 if v >= cut else 0)
    else:
        cut = min(55.0, bg_luma + 25)
        vanishing = lum.point(lambda v: 255 if v <= cut else 0)
    frac = ImageStat.Stat(ImageChops.multiply(vanishing, solid)).sum[0] / 255 / total
    if frac < 0.25:
        return lg
    pad = max(4, int(height * 0.22))
    w, h = lg.width + 2 * pad, lg.height + 2 * pad
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fill = (24, 24, 27, 235) if bg_luma > 128 else (250, 250, 250, 240)
    ImageDraw.Draw(pill).rounded_rectangle((0, 0, w - 1, h - 1), radius=h / 2, fill=fill)
    pill.alpha_composite(lg, (pad, pad))
    return pill


def _rounded_mask(w: int, h: int, r: int) -> Image.Image:
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _shadow(w: int, h: int, r: int, blur: float, alpha: int, pad: int) -> Image.Image:
    sh = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad, pad + w, pad + h), radius=r, fill=alpha)
    return sh.filter(ImageFilter.GaussianBlur(blur))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def source_region(sc: Scene, src_w: int, src_h: int, box: Box) -> tuple[float, float, float, float]:
    """Normalised region of the source to show (focus region, then optional cover crop)."""
    if sc.focus_region is not None:
        x, y, w, h = sc.focus_region.x, sc.focus_region.y, sc.focus_region.w, sc.focus_region.h
        # A small safety margin so crop edges don't slice through UI text at the region's border.
        m = 0.035
        x0, y0 = max(0.0, x - m), max(0.0, y - m)
        x, y, w, h = x0, y0, min(1.0, x + w + m) - x0, min(1.0, y + h + m) - y0
    else:
        x, y, w, h = 0.0, 0.0, 1.0, 1.0
    # A very thin focus strip would float in an empty frame: grow it (symmetrically,
    # inside the image) until it is at most 1.3x wider or taller than the frame's content box.
    box_aspect = box.w / box.h
    aspect = (w * src_w) / max(1e-6, h * src_h)
    if aspect > box_aspect * 1.3:
        nh = min(1.0, (w * src_w) / (box_aspect * 1.3) / src_h)
        y = min(max(y + h / 2 - nh / 2, 0.0), 1.0 - nh)
        h = nh
    elif aspect < box_aspect / 1.3:
        nw = min(1.0, (h * src_h) * box_aspect / 1.3 / src_w)
        x = min(max(x + w / 2 - nw / 2, 0.0), 1.0 - nw)
        w = nw
    if sc.fit_mode == "cover":
        region_aspect = (w * src_w) / max(1e-6, h * src_h)
        box_aspect = box.w / box.h
        fx = min(max(sc.focal_point.x, x), x + w)
        fy = min(max(sc.focal_point.y, y), y + h)
        if region_aspect > box_aspect:  # too wide: narrow it
            nw = w * box_aspect / region_aspect
            nx = min(max(fx - nw / 2, x), x + w - nw)
            x, w = nx, nw
        else:
            nh = h * region_aspect / box_aspect
            ny = min(max(fy - nh / 2, y), y + h - nh)
            y, h = ny, nh
    return x, y, w, h


def fit_contain(src_w: float, src_h: float, box: Box) -> tuple[int, int, int, int]:
    s = min(box.w / src_w, box.h / src_h)
    w = max(2, int(round(src_w * s)))
    h = max(2, int(round(src_h * s)))
    x = int(round(box.x + (box.w - w) / 2))
    y = int(round(box.y + (box.h - h) / 2))
    return x, y, w, h


def to_region(rect: tuple[float, float, float, float], region) -> tuple[float, float, float, float] | None:
    """Map a source-normalised rect into region-normalised coordinates (clipped)."""
    rx, ry, rw, rh = region
    x0 = max(0.0, (rect[0] - rx) / rw)
    y0 = max(0.0, (rect[1] - ry) / rh)
    x1 = min(1.0, (rect[0] + rect[2] - rx) / rw)
    y1 = min(1.0, (rect[1] + rect[3] - ry) / rh)
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return x0, y0, x1 - x0, y1 - y0


def ease_in_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * t)


@dataclass
class CameraPlan:
    motion: str
    focal: tuple[float, float]  # normalised within the camera's frame
    target: tuple[float, float, float, float] | None  # normalised rect for focus zoom
    zoom_to: float

    @property
    def moving(self) -> bool:
        return self.motion != "static"

    def window(self, t: float) -> tuple[float, float, float]:
        """Return (scale, cx, cy) of the visible window at progress t (0..1)."""
        e = ease_in_out(t)
        cx, cy = 0.5, 0.5
        if self.motion == "gentle_push_in":
            s = 1.0 + 0.07 * e
            cx = 0.5 + (self.focal[0] - 0.5) * e
            cy = 0.5 + (self.focal[1] - 0.5) * e
        elif self.motion in ("pan_left", "pan_right"):
            s = 1.10
            d = (0.5 - 0.5 / s) * 0.95
            sign = 1 if self.motion == "pan_left" else -1
            cx = 0.5 + sign * d * (1 - 2 * e)
            cy = self.focal[1]
        elif self.motion == "focus_zoom":
            z = ease_in_out((t - 0.12) / 0.5)
            s = 1.0 + (self.zoom_to - 1.0) * z
            tx, ty = self.focal
            if self.target is not None:
                tx = self.target[0] + self.target[2] / 2
                ty = self.target[1] + self.target[3] / 2
            cx = 0.5 + (tx - 0.5) * z
            cy = 0.5 + (ty - 0.5) * z
        else:
            s = 1.0
        half = 0.5 / s
        cx = min(max(cx, half), 1 - half)
        cy = min(max(cy, half), 1 - half)
        return s, cx, cy


def plan_camera(motion: str, focal: tuple[float, float], target: tuple[float, float, float, float] | None,
                max_zoom: float = 2.0) -> CameraPlan:
    zoom_to = 1.0
    if motion == "focus_zoom":
        if target is not None:
            zoom_to = min(max_zoom, max(1.15, 0.82 / max(target[2], target[3], 0.05)))
        else:
            zoom_to = min(max_zoom, 1.35)
    return CameraPlan(motion=motion, focal=focal, target=target, zoom_to=zoom_to)


# ---------------------------------------------------------------------------
# Scene painters
# ---------------------------------------------------------------------------


def _load_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    return img


def _flatten(img: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    if img.mode != "RGBA":
        return img.convert("RGB")
    bg = Image.new("RGB", img.size, color)
    bg.paste(img, (0, 0), img)
    return bg


def _px_box(region, size) -> tuple[int, int, int, int]:
    x, y, w, h = region
    W, H = size
    return (int(round(x * W)), int(round(y * H)), int(round((x + w) * W)), int(round((y + h) * H)))


def _highlight_layer(size: tuple[int, int], rect_norm, theme: Theme, u: float) -> Image.Image:
    """RGBA layer (content-sized): dims outside the rect and outlines it in the accent colour."""
    W, H = size
    hx, hy, hw, hh = rect_norm[0] * W, rect_norm[1] * H, rect_norm[2] * W, rect_norm[3] * H
    r = int(12 * u)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dim = Image.new("L", (W, H), theme.dim_alpha)
    hole = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hole).rounded_rectangle((hx, hy, hx + hw, hy + hh), radius=r, fill=255)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shade.putalpha(ImageChops.subtract(dim, hole))
    layer.alpha_composite(shade)
    pad = int(6 * u)
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).rounded_rectangle((hx - pad, hy - pad, hx + hw + pad, hy + hh + pad), radius=r + pad,
                                           outline=170, width=max(2, int(10 * u)))
    glow = glow.filter(ImageFilter.GaussianBlur(max(1.0, 9 * u)))
    glow_rgba = Image.new("RGBA", (W, H), theme.accent + (0,))
    glow_rgba.putalpha(glow)
    layer.alpha_composite(glow_rgba)
    ImageDraw.Draw(layer).rounded_rectangle((hx - 2 * u, hy - 2 * u, hx + hw + 2 * u, hy + hh + 2 * u), radius=r,
                                            outline=theme.accent + (255,), width=max(2, int(4 * u)))
    return layer


@dataclass
class MediaParts:
    """Static pieces of an image/clip scene. The camera moves the content inside the card."""

    stage: Image.Image  # output-size background with the card's shadow
    card: tuple[int, int, int, int]  # x, y, w, h (output px)
    mask: Image.Image  # rounded-corner mask, card-sized
    border: Image.Image  # RGBA hairline, card-sized
    region: tuple[float, float, float, float]  # source region shown in the card
    content: Image.Image | None  # images: supersampled RGB content (card * k)
    content_hl: Image.Image | None  # images: content with highlight composited
    hl_layer: Image.Image | None  # clips: card-sized RGBA highlight layer
    camera: CameraPlan


def card_geometry(sc: Scene, media: AssetMedia, lay: Layout) -> tuple[tuple[float, float, float, float], tuple[int, int, int, int]]:
    region = source_region(sc, media.width, media.height, lay.content)
    return region, fit_contain(region[2] * media.width, region[3] * media.height, lay.content)


def paint_media(sc: Scene, sb: Storyboard, media: AssetMedia, lay: Layout, theme: Theme,
                card_y: int | None = None) -> MediaParts:
    W, H = lay.W, lay.H
    stage = _background(theme, W, H).copy()
    region, (cx, cy, cw, ch) = card_geometry(sc, media, lay)
    if card_y is not None:
        cy = card_y
    radius = lay.radius
    pad = int(60 * lay.u)
    sh = _shadow(cw, ch, radius, 34 * lay.u, 150 if theme.dark else 70, pad)
    stage.paste((0, 0, 0), (cx - pad, cy - pad + int(22 * lay.u)), sh)
    mask = _rounded_mask(cw, ch, radius)
    border = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=radius, outline=theme.card_border,
                                             width=max(1, int(2 * lay.u)))

    hl_norm = None
    if sc.highlight is not None:
        hl_norm = to_region((sc.highlight.x, sc.highlight.y, sc.highlight.w, sc.highlight.h), region)
    fp = to_region((sc.focal_point.x - 0.005, sc.focal_point.y - 0.005, 0.01, 0.01), region)
    focal = (fp[0] + fp[2] / 2, fp[1] + fp[3] / 2) if fp else (0.5, 0.5)
    cam = plan_camera(sc.motion, focal, hl_norm, max_zoom=2.0 if media.kind == "image" else 1.5)

    content = content_hl = hl_layer = None
    if media.kind == "image":
        src = _flatten(_load_image(media.path), (245, 245, 244))
        crop = src.crop(_px_box(region, src.size))
        k = cam.zoom_to if cam.motion == "focus_zoom" else (1.12 if cam.moving else 1.0)
        # Never supersample beyond what the source can provide (plus a little headroom).
        k = max(1.0, min(k, max(1.0, crop.width / cw * 1.05)))
        content = crop.resize((max(2, int(round(cw * k))), max(2, int(round(ch * k)))), Image.LANCZOS)
        if hl_norm is not None:
            layer = _highlight_layer(content.size, hl_norm, theme, lay.u * content.width / cw)
            content_hl = content.convert("RGBA")
            content_hl.alpha_composite(layer)
            content_hl = content_hl.convert("RGB")
    elif hl_norm is not None:
        hl_layer = _highlight_layer((cw, ch), hl_norm, theme, lay.u)
    return MediaParts(stage, (cx, cy, cw, ch), mask, border, region, content, content_hl, hl_layer, cam)


def paint_title_stage(sc: Scene, sb: Storyboard, lay: Layout, theme: Theme, logo: AssetMedia | None, k: float) -> Image.Image:
    W, H = int(round(lay.W * k)), int(round(lay.H * k))
    stage = _background(theme, W, H).copy()
    u = lay.u * k
    max_w = W * (0.78 if not lay.portrait else 0.84)
    items: list[tuple[str, object, int]] = []  # (kind, payload, height)
    if logo is not None:
        lg = _load_image(logo.path).convert("RGBA")
        lh = int(110 * u)
        lw = int(lg.width * lh / lg.height)
        if lw > max_w * 0.6:
            lw = int(max_w * 0.6)
            lh = int(lg.height * lw / lg.width)
        lg = logo_for_theme(lg.resize((max(1, lw), max(1, lh)), Image.LANCZOS), theme, lh)
        items.append(("logo", lg, lg.height))
    # The product name is shown once: by the logo if there is one, otherwise as text.
    head = sc.headline or (sb.branding.product_name if sc.role in ("hook", "intro", "cta") and logo is None else "")
    if head:
        blk = layout(head, weight="bold", max_width=max_w, max_lines=3,
                     max_size=int((100 if not lay.portrait else 92) * u), min_size=int(46 * u))
        items.append(("head", blk, blk.height))
    if sc.subline:
        blk = layout(sc.subline, weight="regular", max_width=max_w, max_lines=2, max_size=int(44 * u), min_size=int(28 * u))
        items.append(("sub", blk, blk.height))
    if sc.role == "cta" and sb.branding.cta_text:
        blk = layout(sb.branding.cta_text, weight="semibold", max_width=max_w * 0.8, max_lines=1,
                     max_size=int(42 * u), min_size=int(26 * u))
        items.append(("pill", blk, int(blk.height + 44 * u)))
    if sc.role == "cta" and sb.branding.website_text:
        blk = layout(sb.branding.website_text, weight="regular", max_width=max_w, max_lines=1,
                     max_size=int(36 * u), min_size=int(24 * u))
        items.append(("site", blk, blk.height))
    gap = int(34 * u)
    total = sum(h for _, _, h in items) + gap * max(0, len(items) - 1)
    y = (H - total) / 2
    for kind, payload, h in items:
        if kind == "logo":
            im = payload  # type: ignore[assignment]
            stage.paste(im, (int((W - im.width) / 2), int(y)), im)  # type: ignore[attr-defined]
        elif kind in ("head", "sub", "site"):
            color = theme.text if kind == "head" else theme.subtext
            if kind == "site":
                color = theme.accent if not theme.dark else mix(theme.accent, (255, 255, 255), 0.55)
            draw_block(stage, payload, ((W - max_w) / 2, y), color, align="center", box_width=max_w)  # type: ignore[arg-type]
        elif kind == "pill":
            blk = payload
            pw = blk.width + int(72 * u)  # type: ignore[attr-defined]
            px = (W - pw) / 2
            ImageDraw.Draw(stage).rounded_rectangle((px, y, px + pw, y + h), radius=h / 2, fill=theme.accent)
            draw_block(stage, blk, (px, y + 22 * u), theme.on_accent, align="center", box_width=pw)  # type: ignore[arg-type]
        y += h + gap
    return stage


def _headline_blocks(sc: Scene, lay: Layout, step_no: int | None):
    u = lay.u
    hb = lay.headline
    chip = None
    chip_h = 0
    if step_no is not None:
        chip = layout(f"STEP {step_no}", weight="semibold", max_width=hb.w, max_lines=1, max_size=int(24 * u), min_size=int(16 * u))
        chip_h = chip.height + int(14 * u) + int(16 * u)
    head = sub = None
    head_h = 0.0
    if sc.headline.strip():
        head = layout(sc.headline, weight="bold" if lay.portrait else "semibold", max_width=hb.w,
                      max_lines=lay.headline_max_lines, max_size=lay.headline_size, min_size=lay.headline_min)
        head_h = head.height
        if sc.subline:
            sub = layout(sc.subline, weight="regular", max_width=hb.w, max_lines=1, max_size=int(30 * u), min_size=int(22 * u))
            if head.height + chip_h + sub.height + 8 * u > hb.h:
                sub = None
            else:
                head_h += sub.height + 8 * u
    return chip, head, sub, chip_h + head_h


def headline_overlay(sc: Scene, sb: Storyboard, lay: Layout, theme: Theme, logo: AssetMedia | None, step_no: int | None,
                     head_top: float | None = None):
    """RGBA overlay (never moved by the camera) with headline, step chip and logo."""
    ov = Image.new("RGBA", (lay.W, lay.H), (0, 0, 0, 0))
    u = lay.u
    hb = lay.headline
    chip, head, sub, total_h = _headline_blocks(sc, lay, step_no)
    y = head_top if head_top is not None else hb.y + max(0, (hb.h - total_h) / 2)
    if chip is not None:
        cw = chip.width + int(30 * u)
        ch = chip.height + int(14 * u)
        ImageDraw.Draw(ov).rounded_rectangle((hb.x, y, hb.x + cw, y + ch), radius=ch / 2, fill=theme.accent + (255,))
        draw_block(ov, chip, (hb.x + 15 * u, y + 7 * u), theme.on_accent + (255,))
        y += ch + int(16 * u)
    if head is not None:
        draw_block(ov, head, (hb.x, y), theme.text + (255,))
        y += head.height
        if sub is not None:
            draw_block(ov, sub, (hb.x, y + 8 * u), theme.subtext + (255,))
    if logo is not None:
        lg = _load_image(logo.path).convert("RGBA")
        lb = lay.logo
        s = min(lb.w / lg.width, lb.h / lg.height)
        lw, lh = max(1, int(lg.width * s)), max(1, int(lg.height * s))
        lg = logo_for_theme(lg.resize((lw, lh), Image.LANCZOS), theme, lh)
        lw, lh = lg.size
        if lay.portrait:
            ov.alpha_composite(lg, (int(lb.x + (lb.w - lw) / 2), int(lb.y + (lb.h - lh) / 2)))
        else:
            ov.alpha_composite(lg, (int(lb.x + lb.w - lw), int(lb.y + (lb.h - lh) / 2)))
    return ov, ov.getbbox()


def portrait_positions(sc: Scene, media: AssetMedia, lay: Layout, step_no: int | None) -> tuple[float, int]:
    """Centre headline + card as one group in portrait; returns (headline_top, card_y)."""
    _, (_, _, _, ch) = card_geometry(sc, media, lay)
    _, _, _, head_h = _headline_blocks(sc, lay, step_no)
    gap = 56 * lay.u if head_h else 0
    top, bottom = lay.headline.y, lay.content.y + lay.content.h
    group = head_h + gap + ch
    y0 = top + max(0.0, (bottom - top - group) / 2)
    return y0, int(round(y0 + head_h + gap))


# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------


def _clip_frames(media: AssetMedia, sc: Scene, region, size: tuple[int, int], n: int, timeout: float) -> Iterator[bytes]:
    """Decode up to n frames of the selected clip, normalised to 30 fps and zero-based time."""
    cw, ch = size
    x, y, w, h = region
    vf = (
        f"fps={FPS},crop=iw*{w:.6f}:ih*{h:.6f}:iw*{x:.6f}:ih*{y:.6f},"
        f"scale={cw}:{ch}:flags=lanczos,setsar=1,format=rgb24"
    )
    dur = n / FPS + 0.05
    args = [
        ff.FFMPEG, "-v", "error", "-nostdin", "-ss", f"{sc.clip_in_s or 0.0:.3f}", *ff.SAFE_INPUT,
        "-i", str(media.path), "-t", f"{dur:.3f}", "-an", "-sn", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]  # fmt: skip
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_bytes = cw * ch * 3
    got = 0
    deadline = time.monotonic() + timeout
    try:
        while got < n:
            buf = proc.stdout.read(frame_bytes)  # type: ignore[union-attr]
            if len(buf) < frame_bytes:
                break
            got += 1
            yield buf
            if time.monotonic() > deadline:
                raise RenderError("clip decode timed out")
    finally:
        proc.kill()
        proc.wait()
    if got == 0:
        err = proc.stderr.read().decode("utf-8", "replace")[-800:] if proc.stderr else ""  # type: ignore[union-attr]
        raise RenderError(f"could not decode the selected clip segment: {err}")


def _window_resize(img: Image.Image, size: tuple[int, int], s: float, cx: float, cy: float) -> Image.Image:
    W, H = img.size
    if s == 1.0 and abs(cx - 0.5) < 1e-9 and abs(cy - 0.5) < 1e-9:
        return img if img.size == size else img.resize(size, Image.LANCZOS)
    half = 0.5 / s
    box = ((cx - half) * W, (cy - half) * H, (cx + half) * W, (cy + half) * H)
    return img.resize(size, Image.BICUBIC, box=box)


HL_START, HL_LEN, FADE_FRAMES = int(0.45 * FPS), int(0.35 * FPS), 10


def scene_frames(
    ts: TimelineScene, sc: Scene, sb: Storyboard, media: dict[str, AssetMedia], lay: Layout, step_no: int | None, timeout: float
) -> Iterator[Image.Image]:
    theme = theme_for(sb)
    logo = media.get(sb.branding.logo_asset_id) if sb.branding.logo_asset_id else None
    n = ts.n_frames

    def progress(f: int) -> float:
        return f / max(1, n - 1)

    if sc.source_kind == "title_card":
        cam = plan_camera(sc.motion if sc.motion in ("static", "gentle_push_in") else "gentle_push_in", (0.5, 0.5), None)
        k = 1.08 if cam.moving else 1.0
        stage = paint_title_stage(sc, sb, lay, theme, logo, k)
        if not cam.moving:
            frame = stage if stage.size == (lay.W, lay.H) else stage.resize((lay.W, lay.H), Image.LANCZOS)
            for _ in range(n):
                yield frame
            return
        for f in range(n):
            s, cx, cy = cam.window(progress(f))
            yield _window_resize(stage, (lay.W, lay.H), s, cx, cy)
        return

    am = media[sc.asset_id]  # type: ignore[index]
    head_top = card_y = None
    if lay.portrait:
        head_top, card_y = portrait_positions(sc, am, lay, step_no)
    parts = paint_media(sc, sb, am, lay, theme, card_y=card_y)
    cam = parts.camera
    x, y, cw, ch = parts.card
    ov_img, ov_bbox = headline_overlay(sc, sb, lay, theme, logo, step_no, head_top=head_top)
    ov_crop = ov_img.crop(ov_bbox) if ov_bbox else None
    ov_rgb = ov_crop.convert("RGB") if ov_crop is not None else None
    ov_alpha = ov_crop.getchannel("A") if ov_crop is not None else None

    def compose_frame(card_img: Image.Image, f: int) -> Image.Image:
        frame = parts.stage.copy()
        frame.paste(card_img, (x, y), parts.mask)
        frame.paste(parts.border, (x, y), parts.border)
        if ov_rgb is not None:
            a = min(1.0, (f + 1) / FADE_FRAMES)
            alpha = ov_alpha if a >= 1 else ov_alpha.point(lambda v, a=a: int(v * a))  # type: ignore[union-attr]
            frame.paste(ov_rgb, (ov_bbox[0], ov_bbox[1]), alpha)  # type: ignore[index]
        return frame

    def hl_amount(f: int) -> float:
        return min(1.0, max(0.0, (f - HL_START) / HL_LEN))

    if am.kind == "video":
        clip_n = ts.clip_frames or n
        last: Image.Image | None = None
        f = 0

        def card_from(raw: Image.Image, f: int) -> Image.Image:
            img = raw
            if parts.hl_layer is not None and hl_amount(f) > 0:
                a = hl_amount(f)
                layer = parts.hl_layer
                if a < 1:
                    layer = layer.copy()
                    layer.putalpha(layer.getchannel("A").point(lambda v, a=a: int(v * a)))
                img = raw.convert("RGBA")
                img.alpha_composite(layer)
                img = img.convert("RGB")
            if cam.moving:
                s, cx, cy = cam.window(progress(f))
                img = _window_resize(img, (cw, ch), s, cx, cy)
            return img

        for buf in _clip_frames(am, sc, parts.region, (cw, ch), clip_n, timeout):
            raw = Image.frombuffer("RGB", (cw, ch), buf, "raw", "RGB", 0, 1).copy()
            last = raw
            yield compose_frame(card_from(raw, f), f)
            f += 1
        # Intentional final-frame hold: never loop the recorded workflow.
        held: Image.Image | None = None
        while f < n:
            if held is not None and not cam.moving and f >= max(FADE_FRAMES, HL_START + HL_LEN):
                yield held
            else:
                held = compose_frame(card_from(last, f), f)  # type: ignore[arg-type]
                yield held
            f += 1
        return

    content, content_hl = parts.content, parts.content_hl
    static_frame: Image.Image | None = None
    for f in range(n):
        settled = f >= max(FADE_FRAMES, HL_START + HL_LEN if content_hl is not None else 0)
        if not cam.moving and settled:
            if static_frame is None:
                src = content_hl if content_hl is not None else content
                static_frame = compose_frame(_window_resize(src, (cw, ch), 1.0, 0.5, 0.5), f)  # type: ignore[arg-type]
            yield static_frame
            continue
        src = content
        if content_hl is not None:
            a = hl_amount(f)
            if a >= 1:
                src = content_hl
            elif a > 0:
                src = Image.blend(content, content_hl, a)  # type: ignore[arg-type]
        s, cx, cy = cam.window(progress(f)) if cam.moving else (1.0, 0.5, 0.5)
        yield compose_frame(_window_resize(src, (cw, ch), s, cx, cy), f)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encoder_args(W: int, H: int, crf: int, preset: str, threads: int, out: Path) -> list[str]:
    return [
        ff.FFMPEG, "-v", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-threads", str(threads),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-g", str(FPS * 2), "-an", str(out),
    ]  # fmt: skip


def encode_frames(frames: Iterator[Image.Image], W: int, H: int, out: Path, *, crf: int, preset: str, threads: int,
                  timeout: float, cancel_check: CancelCheck | None) -> int:
    proc = subprocess.Popen(_encoder_args(W, H, crf, preset, threads, out), stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    count = 0
    last_img = None
    last_bytes = b""
    try:
        for img in frames:
            if img is not last_img:
                if img.size != (W, H):
                    raise RenderError(f"frame size {img.size} != {(W, H)}")
                last_bytes = img.tobytes()
                last_img = img
            proc.stdin.write(last_bytes)  # type: ignore[union-attr]
            count += 1
            if count % 15 == 0:
                if cancel_check and cancel_check():
                    raise ff.Canceled()
                if time.monotonic() > deadline:
                    raise RenderError("scene encode timed out")
        proc.stdin.close()  # type: ignore[union-attr]
        rc = proc.wait(timeout=max(5.0, deadline - time.monotonic()))
    except BaseException:
        proc.kill()
        proc.wait()
        close = getattr(frames, "close", None)
        if close:
            close()
        out.unlink(missing_ok=True)
        raise
    if rc != 0:
        err = proc.stderr.read().decode("utf-8", "replace")[-1500:] if proc.stderr else ""  # type: ignore[union-attr]
        out.unlink(missing_ok=True)
        raise RenderError(f"scene encoder failed: {err}")
    return count


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _step_numbers(sb: Storyboard) -> dict[str, int]:
    if sb.style != "guided_walkthrough":
        return {}
    out: dict[str, int] = {}
    n = 0
    for sc in sb.scenes:
        if sc.source_kind != "title_card" and sc.role in ("feature", "step", "showcase"):
            n += 1
            out[sc.id] = n
    return out


def scene_cache_key(sc: Scene, ts: TimelineScene, sb: Storyboard, media: dict[str, AssetMedia], q: Quality, step_no: int | None) -> str:
    logo = media.get(sb.branding.logo_asset_id) if sb.branding.logo_asset_id else None
    asset = media.get(sc.asset_id) if sc.asset_id else None
    payload = {
        "v": RENDERER_VERSION,
        "q": [q.width, q.height, q.seg_crf, q.seg_preset],
        "scene": sc.model_dump(exclude={"narration", "selection_reason", "claims", "origin", "id", "transition_in", "planned_duration_s"}),
        "frames": [ts.n_frames, ts.clip_frames, ts.hold_frames],
        "asset": asset.sha256 if asset else None,
        "logo": logo.sha256 if logo else None,
        "brand": sb.branding.model_dump(exclude={"logo_asset_id"}),
        "style": sb.style,
        "step": step_no,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration_s: float
    frames: int
    has_audio: bool
    size_bytes: int
    scene_cache_hits: int
    scene_cache_misses: int
    warnings: list[str] = field(default_factory=list)


def render(
    sb: Storyboard,
    tl: Timeline,
    media: dict[str, AssetMedia],
    speech_files: dict[str, Path],
    *,
    quality: str,
    out_path: Path,
    work_dir: Path,
    cache_dir: Path,
    threads: int = 2,
    timeout: float = 900,
    progress: ProgressCb | None = None,
    cancel_check: CancelCheck | None = None,
) -> RenderResult:
    q = quality_for(sb, quality)
    lay = make_layout(q.width, q.height)
    steps = _step_numbers(sb)
    work_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = cache_dir / "scenes"
    seg_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def remaining() -> float:
        return max(10.0, timeout - (time.monotonic() - started))

    # Validate that overlays fit before spending time on frames.
    theme = theme_for(sb)
    logo = media.get(sb.branding.logo_asset_id) if sb.branding.logo_asset_id else None
    for sc in sb.scenes:
        try:
            if sc.source_kind == "title_card":
                paint_title_stage(sc, sb, lay, theme, logo, 1.0)
            else:
                headline_overlay(sc, sb, lay, theme, logo, steps.get(sc.id))
        except TextFitError as e:
            raise RenderError(f"Scene {sb.scenes.index(sc) + 1}: {e}. Shorten the text.") from e

    segs: list[Path] = []
    hits = misses = 0
    for i, (ts, sc) in enumerate(zip(tl.scenes, sb.scenes, strict=True)):
        if cancel_check and cancel_check():
            raise ff.Canceled()
        key = scene_cache_key(sc, ts, sb, media, q, steps.get(sc.id))
        seg = seg_dir / f"{key}.mp4"
        if progress:
            progress(f"Rendering scene {i + 1} of {len(sb.scenes)}", i / max(1, len(sb.scenes)))
        if seg.exists() and seg.stat().st_size > 0:
            hits += 1
        else:
            misses += 1
            tmp = work_dir / f"seg_{i:02d}_{uuid.uuid4().hex[:6]}.mp4"
            frames = scene_frames(ts, sc, sb, media, lay, steps.get(sc.id), remaining())
            count = encode_frames(frames, q.width, q.height, tmp, crf=q.seg_crf, preset=q.seg_preset,
                                  threads=threads, timeout=remaining(), cancel_check=cancel_check)
            if count != ts.n_frames:
                tmp.unlink(missing_ok=True)
                raise RenderError(f"scene {i + 1} produced {count} frames, expected {ts.n_frames}")
            os.replace(tmp, seg)
        segs.append(seg)

    if progress:
        progress("Composing final video and narration", None)
    tmp_out = work_dir / f"compose_{uuid.uuid4().hex[:6]}.mp4"
    has_audio = compose(sb, tl, segs, speech_files, q, tmp_out, threads=threads, timeout=remaining(), cancel_check=cancel_check)
    if progress:
        progress("Validating output", None)
    warnings = validate_output(tmp_out, tl, q, has_audio, timeout=remaining(), cancel_check=cancel_check)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_out, out_path)
    info = ff.probe(out_path)
    return RenderResult(
        path=out_path,
        width=info.width,
        height=info.height,
        duration_s=info.duration_s,
        frames=tl.total_frames,
        has_audio=has_audio,
        size_bytes=out_path.stat().st_size,
        scene_cache_hits=hits,
        scene_cache_misses=misses,
        warnings=warnings,
    )


def compose(sb: Storyboard, tl: Timeline, segs: list[Path], speech_files: dict[str, Path], q: Quality, out: Path,
            *, threads: int, timeout: float, cancel_check: CancelCheck | None) -> bool:
    args: list[str] = [ff.FFMPEG, "-v", "error", "-nostdin", "-y"]
    for seg in segs:
        args += [*ff.SAFE_INPUT, "-i", str(seg)]
    audio_inputs: list[tuple[int, int]] = []  # (input index, start sample)
    for ts in tl.scenes:
        if ts.speech is None:
            continue
        p = speech_files.get(ts.scene_id)
        if p is None:
            if sb.narration.mode == "tts":
                raise RenderError("narration audio is missing for a scene; synthesize narration first")
            continue
        audio_inputs.append((len(segs) + len(audio_inputs), ts.speech.start_frame * SAMPLES_PER_FRAME))
        args += [*ff.SAFE_INPUT, "-i", str(p)]

    total_s = tl.total_frames / FPS
    parts: list[str] = []
    for i in range(len(segs)):
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},settb=1/{FPS}[v{i}]")
    acc = "v0"
    acc_frames = tl.scenes[0].n_frames
    for i in range(1, len(segs)):
        ts = tl.scenes[i]
        nxt = f"x{i}"
        if ts.overlap_frames > 0:
            d = ts.overlap_frames / FPS
            off = (acc_frames - ts.overlap_frames) / FPS
            parts.append(f"[{acc}][v{i}]xfade=transition=fade:duration={d:.6f}:offset={off:.6f}[{nxt}]")
            acc_frames += ts.n_frames - ts.overlap_frames
        else:
            parts.append(f"[{acc}][v{i}]concat=n=2:v=1:a=0,settb=1/{FPS}[{nxt}]")
            acc_frames += ts.n_frames
        acc = nxt
    if acc_frames != tl.total_frames:
        raise RenderError(f"timeline mismatch: composed {acc_frames} frames, expected {tl.total_frames}")
    parts.append(f"[{acc}]trim=end_frame={tl.total_frames},setpts=PTS-STARTPTS,format=yuv420p[vout]")
    has_audio = bool(audio_inputs)
    if has_audio:
        labels = []
        for j, (idx, start_sample) in enumerate(audio_inputs):
            parts.append(
                f"[{idx}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS,adelay=delays={start_sample}S:all=1[a{j}]"
            )
            labels.append(f"[a{j}]")
        total_samples = tl.total_frames * SAMPLES_PER_FRAME
        parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
            f"apad=whole_len={total_samples},"
            f"atrim=end_sample={total_samples},asetpts=PTS-STARTPTS[aout]"
        )
    args += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE)]
    args += [
        "-c:v", "libx264", "-preset", q.out_preset, "-crf", str(q.out_crf), "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-threads", str(threads), "-profile:v", "high",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-movflags", "+faststart", "-t", f"{total_s:.6f}", str(out),
    ]  # fmt: skip
    try:
        ff.run(args, timeout=timeout, cancel_check=cancel_check)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return has_audio


def validate_output(path: Path, tl: Timeline, q: Quality, has_audio: bool, *, timeout: float,
                    cancel_check: CancelCheck | None) -> list[str]:
    data = ff.ffprobe_json(path)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RenderError("output has no video stream")
    if v.get("codec_name") != "h264":
        raise RenderError(f"output codec is {v.get('codec_name')}, expected h264")
    if (int(v.get("width", 0)), int(v.get("height", 0))) != (q.width, q.height):
        raise RenderError(f"output is {v.get('width')}x{v.get('height')}, expected {q.width}x{q.height}")
    if v.get("pix_fmt") != "yuv420p":
        raise RenderError(f"output pixel format is {v.get('pix_fmt')}, expected yuv420p")
    if v.get("avg_frame_rate") not in ("30/1", "30000/1000"):
        raise RenderError(f"output frame rate is {v.get('avg_frame_rate')}, expected 30/1")
    expected = tl.total_frames / FPS
    vdur = float(v.get("duration") or data.get("format", {}).get("duration") or 0)
    if abs(vdur - expected) > 2.0 / FPS:
        raise RenderError(f"video duration {vdur:.3f}s differs from timeline {expected:.3f}s")
    if has_audio:
        if a is None or a.get("codec_name") != "aac":
            raise RenderError("narrated output is missing its AAC audio track")
        adur = float(a.get("duration") or 0)
        last_speech_end = max((s.speech.end_frame for s in tl.scenes if s.speech), default=0) / FPS
        if adur + 0.05 < last_speech_end:
            raise RenderError(f"audio ends at {adur:.2f}s before narration finishes at {last_speech_end:.2f}s")
    ff.decode_check(path, timeout=timeout, cancel_check=cancel_check)
    return []


def cleanup_dir(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)
