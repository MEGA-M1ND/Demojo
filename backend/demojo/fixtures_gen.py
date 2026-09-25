"""Deterministic synthetic fixture assets (fictional brands, no private data).

* Tallyfox — a fictional invoicing web app: three screenshots, a short
  screen recording of creating and sending an invoice, and a logo.
* Aurel — a fictional insulated bottle: three illustrated product views and a logo.

Everything is drawn with Pillow from code, so the fixtures are reproducible
and never require a paid API call.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import ffmpeg as ff
from .text_render import FONT_DIR

INK = (17, 24, 39)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)
BG = (248, 250, 252)
BRAND = (234, 88, 12)  # Tallyfox orange
BRAND_SOFT = (255, 237, 213)
GREEN = (22, 163, 74)


def F(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    name = {"regular": "Inter-Regular.otf", "semibold": "Inter-SemiBold.otf", "bold": "Inter-Bold.otf"}[weight]
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _rr(d: ImageDraw.ImageDraw, box, r, **kw):
    d.rounded_rectangle(box, radius=r, **kw)


# ---------------------------------------------------------------------------
# Tallyfox UI
# ---------------------------------------------------------------------------

W_UI, H_UI = 1440, 900


def _chrome(active: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W_UI, H_UI), BG)
    d = ImageDraw.Draw(img)
    # Sidebar
    d.rectangle((0, 0, 232, H_UI), fill=(255, 255, 255))
    d.line((232, 0, 232, H_UI), fill=LINE, width=1)
    _fox_mark(d, 28, 26, 30)
    d.text((68, 30), "Tallyfox", font=F(22, "bold"), fill=INK)
    items = ["Dashboard", "Invoices", "Clients", "Reports", "Settings"]
    for i, it in enumerate(items):
        y = 104 + i * 48
        if it == active:
            _rr(d, (16, y - 8, 216, y + 30), 8, fill=BRAND_SOFT)
            d.text((40, y), it, font=F(17, "semibold"), fill=BRAND)
        else:
            d.text((40, y), it, font=F(17), fill=MUTED)
    # Top bar
    d.rectangle((233, 0, W_UI, 72), fill=(255, 255, 255))
    d.line((233, 72, W_UI, 72), fill=LINE)
    _rr(d, (264, 18, 640, 54), 8, fill=BG, outline=LINE)
    d.text((282, 26), "Search invoices, clients…", font=F(16), fill=MUTED)
    d.ellipse((W_UI - 64, 18, W_UI - 28, 54), fill=(203, 213, 225))
    d.text((W_UI - 55, 25), "MR", font=F(15, "semibold"), fill=INK)
    return img, d


def _fox_mark(d: ImageDraw.ImageDraw, x: int, y: int, s: int):
    d.polygon([(x, y + s * 0.15), (x + s * 0.5, y + s), (x + s, y + s * 0.15), (x + s * 0.72, y + s * 0.4), (x + s * 0.28, y + s * 0.4)], fill=BRAND)


def _button(d, box, label, primary=True, hover=False):
    fill = BRAND if primary else (255, 255, 255)
    if primary and hover:
        fill = (194, 65, 12)
    _rr(d, box, 8, fill=fill, outline=None if primary else LINE)
    f = F(16, "semibold")
    tw = d.textlength(label, font=f)
    d.text(((box[0] + box[2] - tw) / 2, box[1] + (box[3] - box[1] - 20) / 2), label, font=f, fill=(255, 255, 255) if primary else INK)


def ui_dashboard(hover_new: bool = False) -> Image.Image:
    img, d = _chrome("Dashboard")
    d.text((264, 100), "Good morning, Maya", font=F(30, "bold"), fill=INK)
    d.text((264, 144), "Here is where your cash stands this week.", font=F(17), fill=MUTED)
    _button(d, (1236, 100, 1408, 146), "+ New invoice", hover=hover_new)
    cards = [("Outstanding", "$12,480", "8 invoices"), ("Paid this month", "$31,920", "23 invoices"), ("Overdue", "$2,150", "2 invoices")]
    for i, (t, v, sub) in enumerate(cards):
        x = 264 + i * 386
        _rr(d, (x, 196, x + 362, 336), 14, fill=(255, 255, 255), outline=LINE)
        d.text((x + 24, 218), t, font=F(16, "semibold"), fill=MUTED)
        d.text((x + 24, 248), v, font=F(36, "bold"), fill=INK if i != 2 else (220, 38, 38))
        d.text((x + 24, 298), sub, font=F(15), fill=MUTED)
    # Chart
    _rr(d, (264, 360, 1016, 860), 14, fill=(255, 255, 255), outline=LINE)
    d.text((288, 382), "Payments received", font=F(18, "semibold"), fill=INK)
    vals = [42, 55, 38, 64, 71, 58, 80, 76, 92, 88, 97, 104]
    for i, v in enumerate(vals):
        x = 312 + i * 56
        h = v * 3.4
        _rr(d, (x, 820 - h, x + 34, 820), 6, fill=BRAND if i == len(vals) - 1 else (253, 186, 116))
    d.line((300, 821, 990, 821), fill=LINE, width=2)
    # Recent list
    _rr(d, (1040, 360, 1408, 860), 14, fill=(255, 255, 255), outline=LINE)
    d.text((1064, 382), "Recent invoices", font=F(18, "semibold"), fill=INK)
    rows = [("Northwind Studio", "$2,400", "Paid"), ("Blue Heron Co.", "$860", "Sent"), ("Cedar & Pine", "$1,275", "Overdue"),
            ("Orbit Labs", "$3,100", "Paid"), ("Maple Street Cafe", "$420", "Draft")]
    for i, (n, a, s) in enumerate(rows):
        y = 432 + i * 82
        d.text((1064, y), n, font=F(16, "semibold"), fill=INK)
        d.text((1064, y + 26), a, font=F(15), fill=MUTED)
        col = {"Paid": GREEN, "Sent": (37, 99, 235), "Overdue": (220, 38, 38), "Draft": MUTED}[s]
        _rr(d, (1310, y + 4, 1388, y + 34), 15, fill=(255, 255, 255), outline=col)
        tw = d.textlength(s, font=F(14, "semibold"))
        d.text((1349 - tw / 2, y + 10), s, font=F(14, "semibold"), fill=col)
    return img


def ui_invoice_form(progress: float = 1.0, hover_send: bool = False, sent: bool = False) -> Image.Image:
    """Invoice editor; progress (0..1) controls how much of the form is typed in."""
    img, d = _chrome("Invoices")
    d.text((264, 100), "New invoice", font=F(30, "bold"), fill=INK)
    d.text((264, 144), "INV-1042 · Draft", font=F(17), fill=MUTED)
    _button(d, (1092, 100, 1236, 146), "Preview", primary=False)
    _button(d, (1252, 100, 1408, 146), "Send invoice", hover=hover_send)
    _rr(d, (264, 190, 1408, 860), 14, fill=(255, 255, 255), outline=LINE)

    def typed(full: str, start: float, end: float) -> str:
        if progress >= end:
            return full
        if progress <= start:
            return ""
        n = int(len(full) * (progress - start) / (end - start))
        return full[:n]

    fields = [("Client", "Blue Heron Co.", 0.0, 0.25), ("Due date", "Oct 24, 2026", 0.25, 0.4)]
    for i, (label, val, a, b) in enumerate(fields):
        x = 296 + i * 380
        d.text((x, 216), label, font=F(15, "semibold"), fill=MUTED)
        _rr(d, (x, 242, x + 350, 290), 8, fill=BG, outline=BRAND if a < progress < b else LINE, width=2 if a < progress < b else 1)
        d.text((x + 16, 256), typed(val, a, b), font=F(17), fill=INK)
    d.text((296, 330), "Line items", font=F(18, "semibold"), fill=INK)
    d.line((296, 368, 1376, 368), fill=LINE)
    for j, h in enumerate(["Description", "Qty", "Rate", "Amount"]):
        d.text(([296, 900, 1040, 1240][j], 380), h, font=F(14, "semibold"), fill=MUTED)
    items = [("Brand workshop (half day)", "1", "$600", "$600", 0.4, 0.62), ("Landing page copy", "2", "$130", "$260", 0.62, 0.84)]
    for k, (desc, q, r, amt, a, b) in enumerate(items):
        y = 420 + k * 60
        t = typed(desc, a, b)
        d.text((296, y), t, font=F(17), fill=INK)
        if progress >= b:
            d.text((900, y), q, font=F(17), fill=INK)
            d.text((1040, y), r, font=F(17), fill=INK)
            d.text((1240, y), amt, font=F(17, "semibold"), fill=INK)
        d.line((296, y + 40, 1376, y + 40), fill=LINE)
    total = "$860" if progress >= 0.84 else ("$600" if progress >= 0.62 else "$0")
    d.text((1040, 580), "Total", font=F(18, "semibold"), fill=MUTED)
    d.text((1240, 574), total, font=F(26, "bold"), fill=INK)
    d.text((296, 660), "Notes", font=F(15, "semibold"), fill=MUTED)
    _rr(d, (296, 686, 1376, 800), 8, fill=BG, outline=LINE)
    d.text((316, 702), typed("Thanks for the great collaboration!", 0.84, 0.95), font=F(16), fill=INK)
    if sent:
        _rr(d, (1060, 790, 1392, 846), 10, fill=INK)
        d.ellipse((1078, 806, 1102, 830), fill=GREEN)
        d.text((1116, 804), "Invoice sent to Blue Heron", font=F(16, "semibold"), fill=(255, 255, 255))
    return img


def ui_reports() -> Image.Image:
    img, d = _chrome("Reports")
    d.text((264, 100), "Reports", font=F(30, "bold"), fill=INK)
    d.text((264, 144), "Revenue by client · Last 90 days", font=F(17), fill=MUTED)
    _button(d, (1252, 100, 1408, 146), "Export CSV", primary=False)
    _rr(d, (264, 190, 1408, 860), 14, fill=(255, 255, 255), outline=LINE)
    clients = [("Orbit Labs", 9200), ("Northwind Studio", 7400), ("Blue Heron Co.", 5100), ("Cedar & Pine", 3900), ("Maple Street Cafe", 1800)]
    mx = max(v for _, v in clients)
    for i, (n, v) in enumerate(clients):
        y = 240 + i * 110
        d.text((296, y), n, font=F(18, "semibold"), fill=INK)
        w = int(760 * v / mx)
        _rr(d, (296, y + 36, 296 + w, y + 72), 8, fill=BRAND if i == 0 else (253, 186, 116))
        d.text((296 + w + 16, y + 42), f"${v:,}", font=F(17, "semibold"), fill=INK)
    return img


def _cursor(img: Image.Image, x: float, y: float, pressed: bool = False) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    pts = [(x, y), (x, y + 26), (x + 7, y + 20), (x + 12, y + 31), (x + 17, y + 29), (x + 12, y + 18), (x + 21, y + 18)]
    d.polygon(pts, fill=(255, 255, 255), outline=INK)
    if pressed:
        d.ellipse((x - 14, y - 14, x + 14, y + 14), outline=BRAND, width=3)
    return out


def _lerp(a, b, t):
    return a + (b - a) * t


def _ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * min(max(t, 0), 1))


def recording_frames(fps: int = 30):
    """Yield frames of a synthetic ~14 s recording: dashboard -> new invoice -> send."""
    dash = ui_dashboard()
    dash_hover = ui_dashboard(hover_new=True)
    total = 14 * fps
    for f in range(total):
        t = f / fps
        if t < 2.0:  # idle on dashboard, cursor drifts toward button
            e = _ease(t / 2.0)
            frame = _cursor(dash if e < 0.9 else dash_hover, _lerp(760, 1320, e), _lerp(520, 124, e))
        elif t < 2.6:
            frame = _cursor(dash_hover, 1320, 124, pressed=t < 2.3)
        elif t < 10.0:  # typing into the form
            p = (t - 2.6) / 7.2
            cx = _lerp(480, 700, _ease(min(1, p * 2)))
            frame = _cursor(ui_invoice_form(progress=p), cx, _lerp(266, 450, _ease(p)))
        elif t < 11.2:  # move to Send
            e = _ease((t - 10.0) / 1.2)
            frame = _cursor(ui_invoice_form(progress=1.0, hover_send=e > 0.85), _lerp(700, 1330, e), _lerp(450, 124, e))
        elif t < 11.6:
            frame = _cursor(ui_invoice_form(progress=1.0, hover_send=True), 1330, 124, pressed=True)
        else:
            frame = _cursor(ui_invoice_form(progress=1.0, sent=True), 1330, 124)
        yield frame


def write_recording(path: Path, fps: int = 30) -> None:
    args = [
        ff.FFMPEG, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W_UI}x{H_UI}", "-r", str(fps), "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(path),
    ]  # fmt: skip
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    last = None
    last_b = b""
    for fr in recording_frames(fps):
        b = fr.tobytes() if fr is not last else last_b
        proc.stdin.write(b)  # type: ignore[union-attr]
        last, last_b = fr, b
    proc.stdin.close()  # type: ignore[union-attr]
    if proc.wait() != 0:
        raise RuntimeError(proc.stderr.read().decode())  # type: ignore[union-attr]


def tallyfox_logo() -> Image.Image:
    img = Image.new("RGBA", (560, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _rr(d, (0, 10, 120, 130), 28, fill=BRAND)
    d.polygon([(22, 36), (60, 112), (98, 36), (78, 58), (42, 58)], fill=(255, 255, 255))
    d.text((146, 26), "Tallyfox", font=F(82, "bold"), fill=(255, 255, 255))
    return img


# ---------------------------------------------------------------------------
# Aurel bottle (physical product illustrations)
# ---------------------------------------------------------------------------

AUREL = (47, 93, 80)  # deep sage
AUREL_LIGHT = (98, 150, 132)


def _table_bg(W: int, H: int) -> Image.Image:
    top = Image.new("RGB", (W, H), (236, 228, 216))
    grad = Image.linear_gradient("L").resize((W, H))
    bottom = Image.new("RGB", (W, H), (214, 200, 182))
    img = Image.composite(bottom, top, grad)
    d = ImageDraw.Draw(img)
    d.rectangle((0, int(H * 0.72), W, H), fill=(196, 170, 140))
    for i in range(0, W, 7):  # wood grain
        d.line((i, int(H * 0.72), i + 40, H), fill=(188, 162, 132), width=1)
    return img.filter(ImageFilter.GaussianBlur(1.2))


def _bottle(W: int, H: int, view: str) -> Image.Image:
    img = _table_bg(W, H)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = W // 2
    base_y = int(H * 0.80)
    if view == "front":
        bw, bh = 250, 700
    elif view == "side":
        bw, bh = 200, 700
    else:
        bw, bh = 520, 520
    # soft floor shadow
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse((cx - bw * 0.8, base_y - 26, cx + bw * 0.8, base_y + 30), fill=120)
    img.paste((60, 45, 30), (0, 0), sh.filter(ImageFilter.GaussianBlur(22)))
    if view in ("front", "side"):
        x0, x1 = cx - bw // 2, cx + bw // 2
        top = base_y - bh
        _rr(d, (x0, top + 120, x1, base_y), 60, fill=AUREL + (255,))
        _rr(d, (cx - bw * 0.36, top + 40, cx + bw * 0.36, top + 150), 30, fill=AUREL + (255,))
        # cap
        _rr(d, (cx - bw * 0.34, top, cx + bw * 0.34, top + 70), 22, fill=(40, 40, 42, 255))
        if view == "side":
            # carry loop
            d.arc((x1 - 40, top + 4, x1 + 60, top + 90), 270, 90, fill=(40, 40, 42, 255), width=14)
        # highlight stripe
        _rr(d, (x0 + bw * 0.16, top + 170, x0 + bw * 0.26, base_y - 60), 20, fill=AUREL_LIGHT + (160,))
        # wordmark
        f = F(44, "bold")
        word = "AUREL"
        tw = d.textlength(word, font=f)
        if view == "front":
            d.text((cx - tw / 2, top + 380), word, font=f, fill=(236, 228, 216, 255))
            f2 = F(20, "semibold")
            t2 = "750 ML"
            d.text((cx - d.textlength(t2, font=f2) / 2, top + 440), t2, font=f2, fill=(236, 228, 216, 220))
    else:  # top/detail view: cap from above
        r = bw // 2
        cy = base_y - r - 40
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=AUREL + (255,))
        d.ellipse((cx - r * 0.72, cy - r * 0.72, cx + r * 0.72, cy + r * 0.72), fill=(40, 40, 42, 255))
        d.ellipse((cx - r * 0.2, cy - r * 0.2, cx + r * 0.2, cy + r * 0.2), fill=(70, 70, 74, 255))
        d.arc((cx - r * 0.62, cy - r * 0.62, cx + r * 0.62, cy + r * 0.62), 200, 340, fill=(90, 90, 96, 255), width=10)
    img.paste(layer, (0, 0), layer)
    return img


def aurel_logo() -> Image.Image:
    img = Image.new("RGBA", (520, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 10, 120, 130), fill=AUREL)
    d.ellipse((34, 44, 86, 96), fill=(236, 228, 216))
    d.text((146, 24), "Aurel", font=F(84, "bold"), fill=(255, 255, 255))
    return img


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

SOFTWARE_BRIEF = {
    "product_name": "Tallyfox",
    "description": "Tallyfox is invoicing software for freelancers and small studios. You can create an invoice, add line items, and send it to a client from one screen, and the dashboard shows outstanding, paid, and overdue totals.",
    "audience": "Freelancers and small creative studios",
    # Ordered to match the screenshots (dashboard, invoice editor, reports).
    "selling_points": ["See outstanding and overdue totals at a glance", "Create and send an invoice in one screen", "Revenue by client report"],
    "cta_text": "Start free today",
    "website_text": "tallyfox.example",
    "product_type": "software",
    "workflow_notes": "Open the dashboard, click New invoice, fill in the client and line items, then click Send invoice.",
    "accent_color": "#EA580C",
}

PHYSICAL_BRIEF = {
    "product_name": "Aurel",
    "description": "Aurel is a 750 ml insulated steel bottle with a screw cap and a carry loop, in a deep sage finish.",
    "audience": "Commuters and hikers",
    "selling_points": ["750 ml capacity", "Screw cap with carry loop", "Deep sage finish"],
    "cta_text": "Shop Aurel",
    "website_text": "aurel.example",
    "product_type": "physical",
    "workflow_notes": "",
    "accent_color": "#2F5D50",
}


def generate(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sw = out_dir / "software"
    ph = out_dir / "physical"
    sw.mkdir(exist_ok=True)
    ph.mkdir(exist_ok=True)
    ui_dashboard().save(sw / "tallyfox_dashboard.png")
    ui_invoice_form(progress=1.0).save(sw / "tallyfox_new_invoice.png")
    ui_reports().save(sw / "tallyfox_reports.png")
    tallyfox_logo().save(sw / "tallyfox_logo.png")
    write_recording(sw / "tallyfox_recording.mp4")
    for view in ("front", "side", "top"):
        _bottle(1600, 1200, view).save(ph / f"aurel_{view}.jpg", quality=92)
    aurel_logo().save(ph / "aurel_logo.png")
    manifest = {
        "software": {
            "brief": SOFTWARE_BRIEF,
            "images": ["software/tallyfox_dashboard.png", "software/tallyfox_new_invoice.png", "software/tallyfox_reports.png"],
            "recording": "software/tallyfox_recording.mp4",
            "logo": "software/tallyfox_logo.png",
        },
        "physical": {
            "brief": PHYSICAL_BRIEF,
            "images": ["physical/aurel_front.jpg", "physical/aurel_side.jpg", "physical/aurel_top.jpg"],
            "recording": None,
            "logo": "physical/aurel_logo.png",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
