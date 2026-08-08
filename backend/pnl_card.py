"""Dash HQ PnL card renderer.

Pure image compositing (Pillow), no AI generation involved. Layout,
spacing, colors, and fonts are ported directly from the official design
handoff (design_handoff_pnl_card/: README.md, design-elements-spec.md,
stat-grid-spec.md, Dash HQ PNL Card.dc.html) at 2x scale. Sora has no
real italic face on Google Fonts, so the headline number's italic is a
synthetic shear transform, same as what a browser does when you set
font-style:italic on a font without one.
"""

import io
import os
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_HERE = os.path.dirname(__file__)
_FONTS = os.path.join(_HERE, "assets", "fonts")
_LOGO_PATH = os.path.join(_HERE, "assets", "logo_hq.png")

# Exact colors from the design handoff.
BG_TOP = (11, 18, 36)      # #0B1224
BG_MID = (5, 8, 15)        # #05080F
BG_BOTTOM = (4, 5, 9)      # #040509
GHOST = (246, 248, 252)    # #f6f8fc
VALUE_WHITE = (240, 242, 248)  # #f0f2f8
ETH_WHITE = (238, 240, 246)    # #eef0f6
MUTED2 = (90, 106, 138)    # #5A6A8A
USD_MUTED = (138, 155, 191)  # #8A9BBF
DATE_MUTED = (61, 69, 96)  # #3d4560

GREEN = (0, 255, 31)   # #00FF1F profit
RED = (255, 31, 31)    # #FF1F1F loss
BLUE = (31, 111, 255)  # #1F6FFF breakeven

# 2x the 800px design width from the handoff spec.
W, H = 1600, 934
PAD_TOP, PAD_SIDES = 72, 80

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(os.path.join(_FONTS, name), size)
    return _font_cache[key]


def _tw(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _text_size(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int, int, int]:
    tmp = Image.new("L", (10, 10))
    return ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)


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


def _radial_gradient(size, stops, center=(0.5, -0.1), size_frac=(1.3, 1.3)) -> Image.Image:
    # Approximates radial-gradient(120% 90% at 50% -10%, ...) using
    # Pillow's built-in radial_gradient primitive (C-level, fast) scaled
    # and positioned to match, then colorized via a per-channel LUT - no
    # per-pixel Python loop.
    w, h = size
    base = Image.radial_gradient("L").resize((int(w * size_frac[0]), int(h * size_frac[1])), Image.BICUBIC)
    bw, bh = base.size
    cx, cy = w * center[0], h * center[1]
    left, top = int(cx - bw / 2), int(cy - bh / 2)
    canvas = Image.new("L", size, 255)
    canvas.paste(base, (left, top))

    stops = sorted(stops)
    lut_r, lut_g, lut_b = [], [], []
    for i in range(256):
        d = i / 255
        for j in range(len(stops) - 1):
            o0, c0 = stops[j]
            o1, c1 = stops[j + 1]
            if d <= o1 or j == len(stops) - 2:
                t = 0 if o1 == o0 else max(0, min(1, (d - o0) / (o1 - o0)))
                lut_r.append(int(c0[0] + (c1[0] - c0[0]) * t))
                lut_g.append(int(c0[1] + (c1[1] - c0[1]) * t))
                lut_b.append(int(c0[2] + (c1[2] - c0[2]) * t))
                break
    return Image.merge("RGB", (canvas.point(lut_r), canvas.point(lut_g), canvas.point(lut_b)))


def _dot_grid(base: Image.Image, spacing: int = 56, radius: float = 2.2, alpha: int = 10):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(0, base.size[1], spacing):
        for x in range(0, base.size[0], spacing):
            d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, alpha))
    base.alpha_composite(layer)


def _glow(base: Image.Image, center, radius, color, alpha=38, blur=28, fade_frac=0.7):
    # A blurred *solid* circle still reads as a hard-edged blob at large
    # radii - only the outer ~blur px actually soften. The source CSS
    # glow is a radial-gradient that fades to transparent by 70% of its
    # radius *before* any blur, which is what makes it read as ambient
    # wash instead of a circle. Replicate the fade with a real alpha
    # gradient, then blur that.
    size = max(2, int(radius * 2))
    g = Image.radial_gradient("L").resize((size, size), Image.BICUBIC)  # 0 center -> 255 edge
    lut = [int(alpha * (1 - (i / 255) / fade_frac)) if (i / 255) < fade_frac else 0 for i in range(256)]
    alpha_img = g.point(lut)
    circle = Image.new("RGBA", (size, size), (*color, 255))
    circle.putalpha(alpha_img)
    if blur:
        circle = circle.filter(ImageFilter.GaussianBlur(blur))
    x, y = center
    base.alpha_composite(circle, (int(x - size / 2), int(y - size / 2)))


def _corner_brackets(base: Image.Image, color, inset=52, arm=52, width=4, alpha=178):
    _line_alpha(base, [(inset, inset + arm), (inset, inset), (inset + arm, inset)], (*color, alpha), width)
    w, h = base.size
    _line_alpha(base, [(w - inset - arm, inset), (w - inset, inset), (w - inset, inset + arm)], (*color, alpha), width)
    _line_alpha(base, [(inset, h - inset - arm), (inset, h - inset), (inset + arm, h - inset)], (*color, alpha), width)
    _line_alpha(base, [(w - inset - arm, h - inset), (w - inset, h - inset), (w - inset, h - inset - arm)], (*color, alpha), width)


def _diamond(base: Image.Image, center, half, color, glow=True):
    if glow:
        # Matches the spec's tiny "box-shadow 0 0 6px" - a soft edge right
        # at the diamond's own size, not a visible halo around it.
        _glow(base, center, half * 1.6, color, alpha=130, blur=4, fade_frac=0.9)
    x, y = center
    ImageDraw.Draw(base).polygon([(x, y - half), (x + half, y), (x, y + half), (x - half, y)], fill=color)


def _alpha_hex(color, frac: float) -> tuple:
    return (*color, int(255 * frac))


def _rounded_fill(base: Image.Image, box, radius, fill_rgba):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, fill=fill_rgba)
    base.alpha_composite(layer)


def _rounded_outline_alpha(base: Image.Image, box, radius, color_rgba, width):
    # Same rule as _rounded_fill: a translucent stroke drawn straight onto
    # an RGBA image doesn't blend, it just writes that alpha verbatim into
    # the pixel buffer - the final .convert("RGB") on export then discards
    # alpha and reveals it fully opaque. Compositing a transparent layer
    # first is what actually makes it translucent.
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, outline=color_rgba, width=width)
    base.alpha_composite(layer)


def _line_alpha(base: Image.Image, xy, color_rgba, width):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line(xy, fill=color_rgba, width=width, joint="curve")
    base.alpha_composite(layer)


def _skew_italic(im: Image.Image, shear: float = 0.22) -> Image.Image:
    # Sora has no real italic face (confirmed against Google Fonts) - this
    # is exactly what a browser does when font-style:italic is requested
    # on a font without one: a synthetic shear of the upright glyphs.
    w, h = im.size
    new_w = w + int(abs(shear) * h)
    coeffs = (1, shear, -shear * h, 0, 1, 0)
    return im.transform((new_w, h), Image.AFFINE, coeffs, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))


def _glow_text(base: Image.Image, xy, text: str, font, color, glow_alpha=100, glow_blur=30, italic=False):
    ascent, descent = font.getmetrics()
    bbox = _text_size(text, font)
    pad = 24
    tw = bbox[2] - bbox[0]
    mask_w, mask_h = int(tw) + pad * 2, ascent + descent + pad * 2
    mask = Image.new("L", (mask_w, mask_h), 0)
    ImageDraw.Draw(mask).text((pad - bbox[0], pad), text, font=font, fill=255)
    solid = Image.new("RGBA", (mask_w, mask_h), (*color, 255))
    solid.putalpha(mask)
    if italic:
        solid = _skew_italic(solid)
    glow = solid.filter(ImageFilter.GaussianBlur(glow_blur))
    ga = glow.split()[3].point(lambda a: min(255, int(a * glow_alpha / 255)))
    glow.putalpha(ga)
    base.alpha_composite(glow, (int(xy[0]) - pad, int(xy[1]) - pad))
    base.alpha_composite(solid, (int(xy[0]) - pad, int(xy[1]) - pad))
    return tw


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

    # Floor prices arrive from OpenSea as floats, so a genuine tie can land
    # a hair off zero (e.g. 8.000000001e-2 - 8e-2) purely from float
    # representation, not a real price difference. A tiny epsilon keeps
    # true ties labeled BREAK EVEN instead of flipping on noise.
    _EPS = 1e-9
    if pnl_eth > _EPS:
        accent, verdict = GREEN, "REALIZED PROFIT"
    elif pnl_eth < -_EPS:
        accent, verdict = RED, "REALIZED LOSS"
    else:
        accent, verdict = BLUE, "BREAK EVEN"
    sign = "+" if pnl_eth > _EPS else ("-" if pnl_eth < -_EPS else "")

    # ── Background: radial gradient + dot grid + center glow ─────────
    img = _radial_gradient((W, H), [(0.0, BG_TOP), (0.42, BG_MID), (1.0, BG_BOTTOM)]).convert("RGBA")
    _dot_grid(img)
    _glow(img, (W / 2, 88 + 620), 620, accent, alpha=38, blur=28)
    draw = ImageDraw.Draw(img)

    _corner_brackets(img, accent)
    draw = ImageDraw.Draw(img)

    # ── Header: logo + wordmark + status pill ─────────────────────────
    logo_size = 72
    header_y = PAD_TOP
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        mask = Image.new("L", (logo_size, logo_size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, logo_size, logo_size), radius=20, fill=255)
        rounded_logo = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
        rounded_logo.paste(logo, (0, 0), mask)
        img.alpha_composite(rounded_logo, (PAD_SIDES, header_y))
    except FileNotFoundError:
        pass
    draw = ImageDraw.Draw(img)

    wordmark_x = PAD_SIDES + logo_size + 28
    draw.text((wordmark_x, header_y + 2), "DASH HQ", font=_font("Sora-Bold.ttf", 32), fill=GHOST)
    _text_spaced(img, (wordmark_x, header_y + 40), "CITIZEN PNL CARD", _font("Poppins-Regular.ttf", 16), MUTED2, tracking=2.2)
    draw = ImageDraw.Draw(img)

    pill_font = _font("JetBrainsMono-Medium.ttf", 18)
    pill_text_w = _text_spaced_w(draw, verdict, pill_font, 2.1)
    dot_r = 6
    pill_pad_x, pill_pad_y = 28, 12
    pill_gap = 14
    pill_h = pill_font.size + pill_pad_y * 2 + 6
    pill_w = dot_r * 2 + pill_gap + pill_text_w + pill_pad_x * 2
    pill_box = (W - PAD_SIDES - pill_w, header_y, W - PAD_SIDES, header_y + pill_h)
    _rounded_fill(img, pill_box, pill_h / 2, _alpha_hex(accent, 0.12))
    _rounded_outline_alpha(img, pill_box, pill_h / 2, _alpha_hex(accent, 0.30), 2)
    draw = ImageDraw.Draw(img)
    pcy = header_y + pill_h / 2
    _glow(img, (pill_box[0] + pill_pad_x + dot_r, pcy), dot_r * 3, accent, alpha=140, blur=6)
    draw = ImageDraw.Draw(img)
    draw.ellipse((pill_box[0] + pill_pad_x, pcy - dot_r, pill_box[0] + pill_pad_x + dot_r * 2, pcy + dot_r), fill=accent)
    _text_spaced(img, (pill_box[0] + pill_pad_x + dot_r * 2 + pill_gap, pcy - pill_font.size / 1.65), verdict, pill_font, accent, tracking=2.1)
    draw = ImageDraw.Draw(img)

    # ── Hero: synthetic-italic glowing number + upright ETH, USD line ──
    hero_cx = W / 2
    body_top = header_y + logo_size + 44
    num_font = _font("Sora-ExtraBold.ttf", 112)
    unit_font = _font("Sora-Bold.ttf", 46)
    num_text = f"{sign}{abs(pnl_eth):.4f}"
    hero_gap = 28
    num_bbox = _text_size(num_text, num_font)
    unit_bbox = _text_size(symbol, unit_font)
    total_w = (num_bbox[2] - num_bbox[0]) + hero_gap + (unit_bbox[2] - unit_bbox[0])
    start_x = hero_cx - total_w / 2
    hero_y = body_top

    num_ascent, _ = num_font.getmetrics()
    unit_ascent, _ = unit_font.getmetrics()
    baseline_y = hero_y + num_ascent
    unit_y = baseline_y - unit_ascent

    num_w = _glow_text(img, (start_x, hero_y), num_text, num_font, accent, italic=True)
    draw = ImageDraw.Draw(img)
    draw.text((start_x + num_w + hero_gap, unit_y), symbol, font=unit_font, fill=ETH_WHITE)

    usd_font = _font("Poppins-Regular.ttf", 24)
    usd_text = f"≈ {sign}${abs(pnl_usd):,.2f} USD" if pnl_eth != 0 else "≈ $0.00 USD"
    usd_y = baseline_y + 18
    draw.text((hero_cx - _tw(draw, usd_text, usd_font) / 2, usd_y), usd_text, font=usd_font, fill=USD_MUTED)

    # ── Divider ─────────────────────────────────────────────────────
    divider_y = usd_y + 24 + 44
    grad_w = W - PAD_SIDES * 2
    fade = Image.new("L", (grad_w, 1), 0)
    half = grad_w // 2
    for x in range(grad_w):
        d = 1 - abs(x - half) / half
        fade.putpixel((x, 0), int(41 * max(0, d)))  # .16 alpha peak
    fade_rgba = Image.merge("RGBA", (Image.new("L", (grad_w, 1), 255),) * 3 + (fade,))
    img.alpha_composite(fade_rgba, (PAD_SIDES, int(divider_y)))
    draw = ImageDraw.Draw(img)

    # ── Stat panel: content-driven height, one bordered container ─────
    grid_top = divider_y + 44
    grid_x0, grid_x1 = PAD_SIDES, W - PAD_SIDES
    cols = 3
    cell_w = (grid_x1 - grid_x0) / cols
    cell_pad_x, cell_pad_y = 36, 32
    label_font = _font("Poppins-Regular.ttf", 20)
    value_font = _font("Sora-SemiBold.ttf", 34)
    mult_font = _font("Sora-Bold.ttf", 34)
    label_h = 20
    label_gap = 18
    value_h = 34 * 1.2
    cell_h = cell_pad_y * 2 + label_h + label_gap + value_h
    grid_bottom = grid_top + cell_h * 2

    panel_box = (grid_x0, grid_top, grid_x1, grid_bottom)
    _rounded_fill(img, panel_box, 28, _alpha_hex((255, 255, 255), 0.015))
    # Faint highlight on the middle column only (X Handle / Minted),
    # exactly per stat-grid-spec.md's per-cell tint map.
    mid_x0 = grid_x0 + cell_w
    _rounded_fill(img, (mid_x0, grid_top, mid_x0 + cell_w, grid_bottom), 0, _alpha_hex((255, 255, 255), 0.012))
    _rounded_outline_alpha(img, panel_box, 28, _alpha_hex((255, 255, 255), 0.09), 2)
    draw = ImageDraw.Draw(img)
    hairline = _alpha_hex((255, 255, 255), 0.09)
    for col in (1, 2):
        x = grid_x0 + cell_w * col
        _line_alpha(img, (x, grid_top, x, grid_bottom), hairline, 2)
    _line_alpha(img, (grid_x0, grid_top + cell_h, grid_x1, grid_top + cell_h), hairline, 2)
    draw = ImageDraw.Draw(img)

    stats = [
        ("PROJECT", data.get("project", "-"), True, False),
        ("X HANDLE", f"@{data.get('x_username', '-').lstrip('@')}", False, False),
        ("MULTIPLIER", f"{multiplier:.2f}x", False, True),
        ("MINT PRICE", f"{mint_price:.4f} {symbol}", False, False),
        ("MINTED", str(amount), False, False),
        ("FLOOR / ATH", f"{fp:.3f} / {ath:.3f}", False, False),
    ]
    for i, (label, value, show_thumb, colored) in enumerate(stats):
        col, row = i % cols, i // cols
        x0 = grid_x0 + col * cell_w
        y0 = grid_top + row * cell_h
        lcx = x0 + cell_pad_x + 5
        lcy = y0 + cell_pad_y + 7
        _diamond(img, (lcx, lcy), 5, accent)
        draw = ImageDraw.Draw(img)
        _text_spaced(img, (x0 + cell_pad_x + 18, y0 + cell_pad_y), label, label_font, MUTED2, tracking=2)
        draw = ImageDraw.Draw(img)

        vfont = mult_font if colored else value_font
        max_w = cell_w - cell_pad_x * 2
        vtext = value
        while _tw(draw, vtext, vfont) > max_w and len(vtext) > 4:
            vtext = vtext[:-2]
        if vtext != value:
            vtext = vtext[:-1] + "…"
        thumb_offset = 0
        value_y = y0 + cell_pad_y + label_h + label_gap
        if show_thumb and project_thumb_bytes:
            thumb = _circle_thumb(project_thumb_bytes, 30)
            if thumb:
                img.alpha_composite(thumb, (int(x0 + cell_pad_x), int(value_y - 3)))
                draw = ImageDraw.Draw(img)
                thumb_offset = 38
        draw.text((x0 + cell_pad_x + thumb_offset, value_y), vtext, font=vfont, fill=(accent if colored else VALUE_WHITE))

    # ── Footer: two centered lines ─────────────────────────────────────
    footer_font = _font("JetBrainsMono-Regular.ttf", 20)
    sep = "   ·   "
    parts = [(f"{mint_price:.4f} → {fp:.4f} {symbol}", MUTED2), (sep, MUTED2), (f"{pnl_pct:+.1f}%", accent)]
    if eth_usd:
        parts += [(sep, MUTED2), (f"{symbol} ${eth_usd:,.2f}", MUTED2)]
    total_fw = sum(_tw(draw, t, footer_font) for t, _ in parts)
    fx = hero_cx - total_fw / 2
    fy = grid_bottom + 28
    for text, color in parts:
        draw.text((fx, fy), text, font=footer_font, fill=color)
        fx += _tw(draw, text, footer_font)

    date_text = datetime.now(timezone.utc).strftime("%b %-d, %Y") if os.name != "nt" else datetime.now(timezone.utc).strftime("%b %#d, %Y")
    draw.text((hero_cx - _tw(draw, date_text, footer_font) / 2, fy + 32), date_text, font=footer_font, fill=DATE_MUTED)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
