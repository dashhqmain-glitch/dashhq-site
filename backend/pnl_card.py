"""Dash HQ PnL card renderer.

Pure image compositing (Pillow), no AI generation involved - a fixed
Dash HQ-branded template with dynamic text/numbers overlaid, so every
card costs nothing to render beyond CPU time. Layout matches the
approved Figma reference (Dash HQ file, frames 125-4 / 127-6 / 127-63)
pixel-for-pixel where practical; colors otherwise come from styles.css.
"""

import io
import os
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_HERE = os.path.dirname(__file__)
_FONTS = os.path.join(_HERE, "assets", "fonts")
_LOGO_PATH = os.path.join(_HERE, "assets", "logo.png")

BG_TOP = (0, 0, 0)
BG_BOTTOM = (5, 5, 8)
GHOST = (245, 248, 255)
MUTED2 = (150, 165, 199)
MUTED = (98, 113, 145)
ELECTRIC = (77, 114, 255)
TEAL = (34, 211, 238)
# Exact brand accents (user-specified hex): profit/loss/breakeven.
GREEN_LO = (0, 255, 47)
GREEN_HI = (140, 255, 161)
RED_LO = (255, 0, 4)
RED_HI = (255, 140, 142)
BLUE_LO = (0, 4, 255)
BLUE_HI = (140, 142, 255)

# 2x the original 1200x800 canvas - matches the Figma export resolution
# and gives Discord's image preview real sharpness instead of upscaling.
W, H = 2400, 1600
PAD = 130

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(os.path.join(_FONTS, name), size)
    return _font_cache[key]


def _tw(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _linear_gradient(size, color1, color2, angle=135) -> Image.Image:
    w, h = size
    diag = int((w**2 + h**2) ** 0.5) + 4
    grad = Image.linear_gradient("L").resize((diag, diag), Image.BICUBIC)
    grad = grad.rotate(angle, resample=Image.BICUBIC)
    gw, gh = grad.size
    left, top = (gw - w) // 2, (gh - h) // 2
    grad = grad.crop((left, top, left + w, top + h))
    c1 = Image.new("RGB", size, color1)
    c2 = Image.new("RGB", size, color2)
    return Image.composite(c2, c1, grad)


def _text_size(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int, int, int]:
    tmp = Image.new("L", (10, 10))
    return ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)


def _gradient_text(base: Image.Image, xy, text: str, font, color1, color2, angle=90):
    # xy[1] must mean the same thing here as it does for a plain
    # draw.text() call (the font's ascender line), or this text silently
    # sits at a different baseline than anything drawn normally next to
    # it. Sizing the mask off the *tight* glyph bbox (as this used to do)
    # broke that: the tight bbox top rarely equals the ascender line, so
    # gradient text and normal text never actually shared a baseline.
    ascent, descent = font.getmetrics()
    bbox = _text_size(text, font)
    tw = bbox[2] - bbox[0]
    pad = 10
    mask_w, mask_h = int(tw) + pad * 2, ascent + descent + pad * 2
    mask = Image.new("L", (mask_w, mask_h), 0)
    ImageDraw.Draw(mask).text((pad - bbox[0], pad), text, font=font, fill=255)
    grad = _linear_gradient((mask_w, mask_h), color1, color2, angle).convert("RGBA")
    grad.putalpha(mask)
    base.alpha_composite(grad, (int(xy[0]) - pad, int(xy[1]) - pad))
    return tw


def _text_spaced_w(draw, text, font, tracking) -> float:
    if not text:
        return 0
    widths = [draw.textlength(ch, font=font) for ch in text]
    return sum(widths) + tracking * (len(text) - 1)


def _text_spaced(base: Image.Image, xy, text: str, font, fill, tracking: float, center_x: float | None = None):
    draw = ImageDraw.Draw(base)
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2 if center_x is not None else xy[0]
    y = xy[1]
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def _gradient_panel(base: Image.Image, box, radius, color1, color2, angle=135, alpha=255):
    x0, y0, x1, y1 = box
    w, h = int(x1 - x0), int(y1 - y0)
    grad = _linear_gradient((w, h), color1, color2, angle).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=alpha)
    grad.putalpha(mask)
    base.alpha_composite(grad, (int(x0), int(y0)))


def _glow(base: Image.Image, center, radius, color, alpha=90, blur=70):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = center
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(layer)


def _corner_ribbon(base: Image.Image, color1, color2, text, font):
    # Full-bleed diagonal seal in the top-right corner - stretched past
    # both the top and right edges on purpose so it reads as a corner
    # of the card itself, not a floating sticker.
    band_w = 260
    span = int(W * 0.62)
    layer = Image.new("RGBA", (span, span), (0, 0, 0, 0))
    grad = _linear_gradient((span, span), color1, color2, 135).convert("RGBA")
    mask = Image.new("L", (span, span), 0)
    cy = span * 0.5
    ImageDraw.Draw(mask).rectangle((0, cy - band_w / 2, span, cy + band_w / 2), fill=255)
    grad.putalpha(mask)
    layer.alpha_composite(grad)
    d = ImageDraw.Draw(layer)
    tw = _tw(d, text, font)
    d.text((span / 2 - tw / 2, cy - font.size / 1.6), text, font=font, fill=(10, 12, 20))
    layer = layer.rotate(-45, resample=Image.BICUBIC, expand=False)
    base.alpha_composite(layer, (int(W - span / 2 - span * 0.10), int(-span / 2 + span * 0.10)))


def _pill_badge(base: Image.Image, center_x, y, text, font, color, glow=True):
    draw = ImageDraw.Draw(base)
    tw = _tw(draw, text, font)
    pad_x, pad_y = 46, 22
    w, h = tw + pad_x * 2, font.size + pad_y * 2
    box = (center_x - w / 2, y, center_x + w / 2, y + h)
    if glow:
        _glow(base, (center_x, y + h / 2), int(w * 0.62), color, alpha=48, blur=130)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=h / 2, fill=(*color, 28))
    base.alpha_composite(layer)
    ImageDraw.Draw(base).rounded_rectangle(box, radius=h / 2, outline=color, width=3)
    draw = ImageDraw.Draw(base)
    draw.text((center_x - tw / 2, y + pad_y - 2), text, font=font, fill=color)
    return box[3]


def _darken(color, factor=0.45):
    return tuple(int(c * factor) for c in color)


def _icon(base: Image.Image, kind: str, box, color, width=3):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    d = ImageDraw.Draw(base)
    if kind == "image":
        d.rounded_rectangle(box, radius=4, outline=color, width=width)
        d.ellipse((x0 + w * 0.18, y0 + h * 0.18, x0 + w * 0.42, y0 + h * 0.42), outline=color, width=width)
        d.line([(x0 + w * 0.14, y1 - h * 0.22), (x0 + w * 0.42, y0 + h * 0.55),
                (x0 + w * 0.62, y0 + h * 0.72), (x1 - w * 0.12, y0 + h * 0.38)], fill=color, width=width, joint="curve")
    elif kind == "person":
        d.ellipse((cx - w * 0.2, y0 + h * 0.04, cx + w * 0.2, y0 + h * 0.44), outline=color, width=width)
        d.arc((x0 + w * 0.04, y0 + h * 0.42, x1 - w * 0.04, y1 + h * 0.38), start=180, end=360, fill=color, width=width)
    elif kind == "tag":
        pts = [(x0 + w * 0.1, cy), (cx, y0), (x1 - w * 0.08, y0 + h * 0.14),
               (x1 - w * 0.08, y1 - h * 0.14), (cx, y1), (x0 + w * 0.1, cy)]
        d.line(pts, fill=color, width=width, joint="curve")
        d.ellipse((cx - 3.5, y0 + h * 0.3, cx + 3.5, y0 + h * 0.3 + 7), fill=color)
    elif kind == "layers":
        for dy in (0, 0.32, 0.64):
            yy = y0 + h * dy
            d.line([(x0, yy + h * 0.18), (cx, yy), (x1, yy + h * 0.18)], fill=color, width=width, joint="curve")
    elif kind == "trend":
        d.line([(x0, y1 - h * 0.08), (x0 + w * 0.32, y0 + h * 0.5), (x0 + w * 0.56, y0 + h * 0.62),
                (x1 - w * 0.06, y0 + h * 0.02)], fill=color, width=width, joint="curve")
        d.line([(x1 - w * 0.3, y0), (x1 - w * 0.02, y0), (x1 - w * 0.02, y0 + h * 0.3)], fill=color, width=width, joint="curve")
    elif kind == "check":
        d.ellipse(box, outline=color, width=width)
        d.line([(x0 + w * 0.24, cy), (x0 + w * 0.44, y1 - h * 0.26), (x1 - w * 0.2, y0 + h * 0.26)],
               fill=color, width=width, joint="curve")


def _stat_panel(base: Image.Image, box, radius, stroke_color=(255, 255, 255), fill=(16, 19, 26, 235)):
    # Plain solid panel - flat fill, clean rounded corners, a colored
    # border. No blur/translucency tricks.
    # Supersample the mask/outline at 4x then downscale - this is what
    # actually makes the rounded corners look smooth instead of faceted;
    # ImageDraw's own antialiasing on a rounded_rectangle stroke is weak
    # at the exact radius sizes used here.
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    ss = 4

    mask_big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask_big).rounded_rectangle((0, 0, w * ss, h * ss), radius=radius * ss, fill=255)
    mask = mask_big.resize((w, h), Image.LANCZOS)
    panel = Image.new("RGBA", (w, h), fill)
    base.paste(panel, (x0, y0), mask)

    edge_big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    ImageDraw.Draw(edge_big).rounded_rectangle((0, 0, w * ss, h * ss), radius=radius * ss, outline=(*stroke_color, 220), width=3 * ss)
    edge = edge_big.resize((w, h), Image.LANCZOS)
    base.alpha_composite(edge, (x0, y0))


def _grain(base: Image.Image, opacity: int = 7) -> Image.Image:
    noise = Image.effect_noise(base.size, 28).convert("L")
    noise_rgba = Image.merge("RGBA", (noise, noise, noise, Image.new("L", base.size, opacity)))
    out = base.convert("RGBA")
    out.alpha_composite(noise_rgba)
    return out


def _circle_thumb(img_bytes: bytes, size: int) -> Image.Image | None:
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception:
        return None
    side = min(im.size)
    left, top = (im.width - side) // 2, (im.height - side) // 2
    im = im.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def render_pnl_card(data: dict, project_thumb_bytes: bytes | None = None) -> bytes:
    symbol = data.get("symbol") or "ETH"
    mint_price = float(data["mint_price"])
    amount = int(data["amount_minted"])
    fp = float(data["fp"])
    ath = float(data.get("ath") or fp)
    eth_usd = float(data.get("eth_usd") or 0)

    pnl_eth = (fp - mint_price) * amount
    pnl_pct = ((fp - mint_price) / mint_price * 100) if mint_price > 0 else 0.0
    multiplier = (fp / mint_price) if mint_price > 0 else 0.0
    pnl_usd = pnl_eth * eth_usd

    if pnl_eth > 0:
        lo, hi, verdict, eyebrow = GREEN_LO, GREEN_HI, "PROFIT", "REALIZED PROFIT"
    elif pnl_eth < 0:
        lo, hi, verdict, eyebrow = RED_LO, RED_HI, "LOSS", "REALIZED LOSS"
    else:
        lo, hi, verdict, eyebrow = BLUE_LO, BLUE_HI, "EVEN", "BREAK EVEN"
    accent = lo
    sign = "+" if pnl_eth > 0 else ("-" if pnl_eth < 0 else "")

    # ── Background: diagonal base gradient + brand glow + outcome wash ──
    img = _linear_gradient((W, H), BG_TOP, BG_BOTTOM, angle=115).convert("RGBA")
    _glow(img, (W * 0.16, H * 0.28), 620, ELECTRIC, alpha=55, blur=200)
    _glow(img, (W * 0.30, H * 0.55), 440, TEAL, alpha=30, blur=200)
    _glow(img, (W * 0.5, H * 0.42), 620, accent, alpha=50, blur=260)
    img = _grain(img, opacity=8)

    draw = ImageDraw.Draw(img)

    # ── Top: logo lockup ─────────────────────────────────────────────
    logo_size = 172
    logo_y = 148
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        img.alpha_composite(logo, (PAD, logo_y))
    except FileNotFoundError:
        pass
    draw = ImageDraw.Draw(img)
    draw.text((PAD + logo_size + 44, logo_y + 24), "DASH HQ", font=_font("Geist-ExtraBold.ttf", 68), fill=GHOST)
    _text_spaced(img, (PAD + logo_size + 44, logo_y + 110), "CITIZEN PNL CARD", _font("GeistMono-Bold.ttf", 27), MUTED2, tracking=4)
    draw = ImageDraw.Draw(img)

    # ── Verdict ribbon, full-bleed top-right corner ─────────────────
    _corner_ribbon(img, lo, hi, verdict, _font("Geist-ExtraBold.ttf", 60))
    draw = ImageDraw.Draw(img)

    # ── Hero: pill badge + giant number ──────────────────────────────
    hero_cx = W / 2
    pill_bottom = _pill_badge(img, hero_cx, 330, eyebrow, _font("Geist-ExtraBold.ttf", 34), accent)
    draw = ImageDraw.Draw(img)

    num_font = _font("GeistMono-ExtraBold.ttf", 156)
    unit_font = _font("Geist-ExtraBold.ttf", 140)
    num_text = f"{sign}{abs(pnl_eth):.4f}"
    gap = 40
    num_bbox = _text_size(num_text, num_font)
    unit_bbox = _text_size(symbol, unit_font)
    total_w = (num_bbox[2] - num_bbox[0]) + gap + (unit_bbox[2] - unit_bbox[0])
    start_x = hero_cx - total_w / 2
    hero_y = pill_bottom + 70

    # Align by baseline, not by the top of each font's bounding box - two
    # different typefaces at two different sizes never share a cap-height,
    # so a fixed pixel offset between them drifts out of alignment.
    num_ascent, _ = num_font.getmetrics()
    unit_ascent, _ = unit_font.getmetrics()
    baseline_y = hero_y + num_ascent
    unit_y = baseline_y - unit_ascent

    num_w = _gradient_text(img, (start_x, hero_y), num_text, num_font, hi, lo, angle=90)
    draw = ImageDraw.Draw(img)
    draw.text((start_x + num_w + gap, unit_y), symbol, font=unit_font, fill=GHOST)

    usd_font = _font("Geist-SemiBold.ttf", 44)
    usd_text = f"≈ {sign}${abs(pnl_usd):,.2f} USD" if pnl_eth != 0 else "≈ $0.00 USD"
    usd_y = hero_y + 210
    draw.text((hero_cx - _tw(draw, usd_text, usd_font) / 2, usd_y), usd_text, font=usd_font, fill=GHOST)

    # ── Stat grid: 3 columns x 2 rows of glass chips ─────────────────
    grid_top = usd_y + 140
    grid_bottom = H - 220
    cols, rows_n = 3, 2
    gutter = 40
    grid_x0, grid_x1 = PAD, W - PAD
    cell_w = (grid_x1 - grid_x0 - gutter * (cols - 1)) / cols
    cell_h = (grid_bottom - grid_top - gutter * (rows_n - 1)) / rows_n

    stats = [
        ("PROJECT", data.get("project", "-"), True, False, "image"),
        ("X HANDLE", f"@{data.get('x_username', '-').lstrip('@')}", False, False, "person"),
        ("MULTIPLIER", f"{multiplier:.2f}x", False, True, "trend"),
        ("MINT PRICE", f"{mint_price:.4f} {symbol}", False, False, "tag"),
        ("MINTED", str(amount), False, False, "layers"),
        ("FLOOR / ATH", f"{fp:.3f} / {ath:.3f}", False, False, "check"),
    ]
    label_font = _font("GeistMono-Bold.ttf", 26)
    value_font = _font("Geist-ExtraBold.ttf", 48)
    icon_size = 30
    for i, (label, value, show_thumb, colored, icon_kind) in enumerate(stats):
        col, row = i % cols, i // cols
        x0 = grid_x0 + col * (cell_w + gutter)
        y0 = grid_top + row * (cell_h + gutter)
        box = (x0, y0, x0 + cell_w, y0 + cell_h)
        _stat_panel(img, box, 30, stroke_color=_darken(accent, 0.5))
        draw = ImageDraw.Draw(img)
        _icon(img, icon_kind, (x0 + 40, y0 + 32, x0 + 40 + icon_size, y0 + 32 + icon_size), MUTED2)
        draw = ImageDraw.Draw(img)
        _text_spaced(img, (x0 + 40 + icon_size + 16, y0 + 34), label, label_font, MUTED2, tracking=3)
        draw = ImageDraw.Draw(img)

        max_w = cell_w - 80
        vtext = value
        while _tw(draw, vtext, value_font) > max_w and len(vtext) > 4:
            vtext = vtext[:-2]
        if vtext != value:
            vtext = vtext[:-1] + "…"
        thumb_offset = 0
        if show_thumb and project_thumb_bytes:
            thumb = _circle_thumb(project_thumb_bytes, 48)
            if thumb:
                img.alpha_composite(thumb, (int(x0 + 40), int(y0 + cell_h - 84)))
                draw = ImageDraw.Draw(img)
                thumb_offset = 62
        draw.text((x0 + 40 + thumb_offset, y0 + cell_h - 92), vtext, font=value_font, fill=(accent if colored else GHOST))

    # ── Footer stat line + date ───────────────────────────────────────
    footer_font = _font("GeistMono-Regular.ttf", 26)
    fx = PAD
    fy = H - 106
    draw.text((fx, fy), f"{mint_price:.4f} → {fp:.4f} {symbol}", font=footer_font, fill=MUTED)
    fx += _tw(draw, f"{mint_price:.4f} → {fp:.4f} {symbol}", footer_font) + 26
    draw.text((fx, fy), "|", font=footer_font, fill=(70, 82, 108))
    fx += _tw(draw, "|", footer_font) + 26
    pct_text = f"{pnl_pct:+.1f}%"
    draw.text((fx, fy), pct_text, font=footer_font, fill=accent)
    fx += _tw(draw, pct_text, footer_font) + 26
    if eth_usd:
        draw.text((fx, fy), "|", font=footer_font, fill=(70, 82, 108))
        fx += _tw(draw, "|", footer_font) + 26
        draw.text((fx, fy), f"{symbol} ${eth_usd:,.2f}", font=footer_font, fill=MUTED)

    date_text = datetime.now(timezone.utc).strftime("%b %-d, %Y") if os.name != "nt" else datetime.now(timezone.utc).strftime("%b %#d, %Y")
    draw.text((W - PAD - _tw(draw, date_text, footer_font), fy), date_text, font=footer_font, fill=MUTED)

    # No second grain pass here on purpose - the grain already baked into
    # the backdrop (before the chips were drawn) is what makes each
    # chip's blur step visibly smooth something. A uniform pass now would
    # re-sharpen the chip interiors and erase that contrast.

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
