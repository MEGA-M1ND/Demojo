"""Deterministic text layout and drawing with Pillow.

User text is only ever drawn into images here; it never reaches a shell or an
FFmpeg filter string. Glyph coverage is checked against the bundled fonts'
character maps so unsupported characters are reported instead of silently
rendered as boxes.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"

# weight -> ordered list of font files (primary first, then fallbacks)
FONT_FILES = {
    "regular": ["Inter-Regular.otf", "DejaVuSans.ttf"],
    "semibold": ["Inter-SemiBold.otf", "DejaVuSans-Bold.ttf"],
    "bold": ["Inter-Bold.otf", "DejaVuSans-Bold.ttf"],
}

# Characters we deliberately treat as whitespace / ignorable when checking coverage.
_IGNORABLE = {"​", "‌", "‍", "️", "︎"}


class TextFitError(ValueError):
    pass


@cache
def _cmap(filename: str) -> frozenset[int]:
    font = TTFont(str(FONT_DIR / filename), lazy=True)
    cmap = font.getBestCmap() or {}
    return frozenset(cmap.keys())


@lru_cache(maxsize=256)
def _font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / filename), size=size)


def normalize_text(text: str) -> str:
    """NFC-normalise, map control characters/newlines to spaces, collapse runs."""
    text = unicodedata.normalize("NFC", text or "")
    out = []
    for ch in text:
        if ch in _IGNORABLE:
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C") or ch in "\n\r\t  ":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def unsupported_chars(text: str) -> set[str]:
    """Characters that none of the bundled fonts can draw."""
    missing: set[str] = set()
    files = FONT_FILES["regular"] + FONT_FILES["bold"]
    maps = [_cmap(f) for f in dict.fromkeys(files)]
    for ch in normalize_text(text):
        if ch == " ":
            continue
        cp = ord(ch)
        if not any(cp in m for m in maps):
            missing.add(ch)
    return missing


def _pick_file(ch: str, weight: str) -> str:
    cp = ord(ch)
    for fn in FONT_FILES[weight]:
        if cp in _cmap(fn):
            return fn
    return FONT_FILES[weight][0]


def _runs(text: str, weight: str) -> list[tuple[str, str]]:
    """Split text into (font_file, substring) runs for glyph fallback."""
    runs: list[tuple[str, str]] = []
    for ch in text:
        if ch == " ":
            fn = runs[-1][0] if runs else FONT_FILES[weight][0]
        else:
            fn = _pick_file(ch, weight)
        if runs and runs[-1][0] == fn:
            runs[-1] = (fn, runs[-1][1] + ch)
        else:
            runs.append((fn, ch))
    return runs


def text_width(text: str, weight: str, size: int) -> float:
    return sum(_font(fn, size).getlength(s) for fn, s in _runs(text, weight))


def _wrap(words: list[str], weight: str, size: int, max_width: float) -> list[str] | None:
    lines: list[str] = []
    cur = ""
    for w in words:
        if text_width(w, weight, size) > max_width:
            # Break an over-long token (URLs, long compound words) by characters.
            if cur:
                lines.append(cur)
                cur = ""
            piece = ""
            for ch in w:
                if text_width(piece + ch, weight, size) > max_width and piece:
                    lines.append(piece)
                    piece = ch
                else:
                    piece += ch
            cur = piece
            continue
        cand = f"{cur} {w}" if cur else w
        if text_width(cand, weight, size) <= max_width:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


@dataclass
class TextBlock:
    lines: list[str]
    size: int
    weight: str
    line_height: int
    width: int
    height: int


def layout(text: str, *, weight: str, max_width: float, max_lines: int, max_size: int, min_size: int) -> TextBlock:
    """Find the largest font size (<= max_size) at which text fits in max_lines."""
    text = normalize_text(text)
    if not text:
        return TextBlock([], max_size, weight, int(max_size * 1.2), 0, 0)
    words = text.split(" ")
    size = max_size
    while size >= min_size:
        lines = _wrap(words, weight, size, max_width)
        if lines is not None and len(lines) <= max_lines:
            lh = int(round(size * 1.22))
            width = int(max(text_width(ln, weight, size) for ln in lines)) + 1
            return TextBlock(lines, size, weight, lh, width, lh * len(lines))
        size -= 2
    raise TextFitError(f"text does not fit in {max_lines} line(s): {text[:60]!r}")


def draw_block(
    img: Image.Image,
    block: TextBlock,
    xy: tuple[float, float],
    fill: tuple[int, int, int, int] | tuple[int, int, int],
    align: str = "left",
    box_width: float | None = None,
) -> None:
    """Draw a laid-out block with its top-left at xy (baseline-aligned runs)."""
    draw = ImageDraw.Draw(img)
    x0, y0 = xy
    ascent = _font(FONT_FILES[block.weight][0], block.size).getmetrics()[0]
    for i, line in enumerate(block.lines):
        lw = text_width(line, block.weight, block.size)
        if align == "center" and box_width is not None:
            x = x0 + (box_width - lw) / 2
        elif align == "right" and box_width is not None:
            x = x0 + box_width - lw
        else:
            x = x0
        baseline = y0 + i * block.line_height + ascent + (block.line_height - block.size * 1.22) / 2
        for fn, s in _runs(line, block.weight):
            f = _font(fn, block.size)
            draw.text((x, baseline), s, font=f, fill=fill, anchor="ls")
            x += f.getlength(s)
