"""User text is only ever drawn with Pillow; it never reaches a shell or FFmpeg argument."""

from __future__ import annotations

import pytest
from PIL import Image

from demojo.text_render import TextFitError, draw_block, layout, normalize_text, unsupported_chars

ATTACKS = [
    'Say "hello" — it’s «quoted» & <b>bold</b>',
    "$(rm -rf /) `whoami` ; DROP TABLE projects; --",
    "../../etc/passwd C:\\Windows\\System32\\cmd.exe",
    "drawtext=text='%{pts}':fontfile=/etc/shadow",
    "Ünïcödé naïve café — Ελληνικά, Кириллица ✓ ½ € ™",
    "line one\nline two\ttabbed\x00null\x1bescape",
    "-y -i /dev/zero -f null -",
    "https://evil.example/?q=1&x=%0a%0d",
]


@pytest.mark.parametrize("text", ATTACKS)
def test_attack_strings_render_safely(text):
    clean = normalize_text(text)
    assert "\n" not in clean and "\x00" not in clean and "\x1b" not in clean
    assert not unsupported_chars(text), unsupported_chars(text)
    block = layout(text, weight="semibold", max_width=1400, max_lines=3, max_size=56, min_size=20)
    img = Image.new("RGB", (1600, 400), (10, 10, 10))
    draw_block(img, block, (40, 40), (255, 255, 255))
    assert img.getbbox() is not None  # something was drawn


def test_emoji_and_cjk_are_reported_not_silently_boxed():
    # English is the initial scope; glyphs outside the bundled fonts are rejected with a message.
    assert unsupported_chars("Ship it 🚀") == {"🚀"}
    assert unsupported_chars("中文") == {"中", "文"}


def test_text_too_long_for_box_raises():
    with pytest.raises(TextFitError):
        layout("word " * 200, weight="bold", max_width=300, max_lines=2, max_size=40, min_size=30)


def test_long_tokens_are_broken_not_overflowing():
    blk = layout("x" * 120, weight="regular", max_width=200, max_lines=10, max_size=24, min_size=12)
    assert len(blk.lines) > 1
