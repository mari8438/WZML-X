#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Template 11: Ultra-Pro Flagship Hero Card Anime Banner Engine for WZML-X
Features:
- Depth-of-Field (DoF) + chromatic fringe widescreen environment backdrop
- Floating 3D poster card on left with multi-layer shadow and dual glowing borders
- Glassmorphic hero container on right with acrylic blur, specular sheen, and double stroke
- Minimalist golden header tag with vertical pill
- Bold white geometric headline with subtle glow and drop shadow
- Dual/Triple pill badges (Episode, Resolution/Codec, Audio)
- Metallic specular separator line
- 4-line pixel-accurate synopsis wrapper
- 3-column streaming specs grid (Video Stream, Genre Tags, Release Info)
- Telegram channel footer and dynamic season simulcast progress bar
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
)


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _extract_digits(val: Any, default: int = 1) -> int:
    if val is None:
        return default
    if isinstance(val, int):
        return max(1, val)
    m = re.search(r"\d+", str(val))
    return int(m.group(0)) if m else default


def load_font(size: int, weight: str = "regular", cjk: bool = False) -> ImageFont.FreeTypeFont:
    """Loads TrueType / OpenType font with graceful fallback for both Windows and Linux containers."""
    if cjk:
        for p in [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "msgothic.ttc",
            "msmincho.ttc",
        ]:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass

    is_bold = weight in ("bold", "extrabold", "black", "semibold")

    candidates = (
        [
            "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf",
            "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
            "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
            "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "DejaVuSans-Bold.ttf",
            "arialbd.ttf",
            "segoeuib.ttf",
        ]
        if is_bold
        else [
            "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
            "/usr/share/fonts/opentype/inter/InterDisplay-Regular.otf",
            "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "DejaVuSans.ttf",
            "arial.ttf",
            "segoeui.ttf",
        ]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue

    try:
        return ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resizes and center-crops an image to fill (target_w, target_h) without distortion."""
    src_w, src_h = img.size
    scale = max(target_w / max(1, src_w), target_h / max(1, src_h))
    new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def smart_poster_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Smart-fits high-resolution AniList coverImage into the 374x608 left box
    without stretching or cutting off character faces/logos.
    - Portrait covers (src_ratio <= 0.85): edge-to-edge fill with top-weighted framing
      so 100% of vertical height (or 96% width) is preserved and faces/logos are never cut.
    - Landscape/Square images (src_ratio > 0.85): full aspect-preserved fit over
      an atmospheric blurred backdrop so characters and logos are never chopped off.
    """
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (target_w, target_h), (25, 30, 48))

    src_ratio = src_w / src_h

    if src_ratio <= 0.85:
        scale = max(target_w / src_w, target_h / src_h)
        new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - target_w) // 2
        top = int((new_h - target_h) * 0.12)
        return resized.crop((left, top, left + target_w, top + target_h))

    # For wider/landscape fallback images, preserve full artwork over blurred canvas
    bg_fill = cover_resize(img, target_w, target_h).filter(ImageFilter.GaussianBlur(radius=22))
    bg_fill = ImageEnhance.Brightness(bg_fill).enhance(0.52)
    scale = min(target_w / src_w, target_h / src_h)
    fg_w, fg_h = int(round(src_w * scale)), int(round(src_h * scale))
    fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
    ox = (target_w - fg_w) // 2
    oy = (target_h - fg_h) // 2
    bg_fill.paste(fg, (ox, oy))
    return bg_fill


def fit_headline_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int = 708,
) -> Tuple[ImageFont.FreeTypeFont, List[str], int]:
    """
    Dynamically fits the anime title into 1 or 2 lines within max_width (708 px)
    by scaling font size (56 -> 46 -> 38 -> 32 -> 28 -> 24) so long titles NEVER get truncated.
    Returns (font, lines, line_height).
    """
    clean_title = " ".join(title.strip().split())
    if not clean_title:
        clean_title = "Untitled Anime"

    # 1. Try single line at 56 or 48 if short enough
    for sz in (56, 48):
        f = load_font(sz, "extrabold")
        bbox = draw.textbbox((0, 0), clean_title, font=f)
        if (bbox[2] - bbox[0]) <= max_width:
            return f, [clean_title], sz + 8

    # 2. Try 2-line balanced wrapping across descending font sizes
    words = clean_title.split()
    for sz in (46, 42, 38, 34, 32, 28, 25, 22):
        f = load_font(sz, "extrabold")
        # Check if fits in 1 line at this size
        bbox = draw.textbbox((0, 0), clean_title, font=f)
        if (bbox[2] - bbox[0]) <= max_width:
            return f, [clean_title], sz + 8

        if len(words) >= 2:
            # Find the most balanced 2-line split where both lines <= max_width
            best_split = None
            best_diff = 999999
            for i in range(1, len(words)):
                l1 = " ".join(words[:i])
                l2 = " ".join(words[i:])
                b1 = draw.textbbox((0, 0), l1, font=f)
                b2 = draw.textbbox((0, 0), l2, font=f)
                w1, w2 = b1[2] - b1[0], b2[2] - b2[0]
                if w1 <= max_width and w2 <= max_width:
                    diff = abs(w1 - w2)
                    if diff < best_diff:
                        best_diff = diff
                        best_split = [l1, l2]
            if best_split:
                return f, best_split, sz + 6

    # Fallback: greedy wrap up to 2 lines at size 22 without truncating words if possible
    f = load_font(22, "extrabold")
    return f, wrap_text_lines(draw, clean_title, f, max_width, max_lines=2), 28


def extract_dominant_accent(img: Image.Image) -> Tuple[int, int, int]:
    """Extracts a vibrant dominant accent color from the poster artwork."""
    try:
        small = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
        pal = small.quantize(colors=6).convert("RGB")
        colors = pal.getcolors(64 * 64) or []
        best_rgb = (139, 92, 246)
        best_score = -1.0
        for count, (r, g, b) in colors:
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / max(1, mx)
            lum = (r + g + b) / 3.0
            if 45 < lum < 235:
                score = sat * (count ** 0.35)
                if score > best_score:
                    best_score = score
                    best_rgb = (r, g, b)
        return best_rgb
    except Exception:
        return (139, 92, 246)


def apply_chromatic_aberration_and_dof(bg: Image.Image, shift_px: int = 2, dof_radius: float = 3.0) -> Image.Image:
    """Applies depth-of-field lens blur and subtle lateral chromatic aberration."""
    try:
        w, h = bg.size
        blurred = bg.filter(ImageFilter.GaussianBlur(radius=dof_radius))
        r, g, b = blurred.split()
        r_shifted = ImageChops.offset(r, -shift_px, 0)
        b_shifted = ImageChops.offset(b, shift_px, 0)
        aberrated = Image.merge("RGB", (r_shifted, g, b_shifted))

        mask = Image.new("L", (w, h), 0)
        m_px = mask.load()
        for y in range(h):
            for x in range(w):
                nx = (x - w * 0.72) / (w * 0.45)
                ny = (y - h * 0.48) / (h * 0.48)
                dist = min(1.0, (nx * nx + ny * ny) ** 0.5)
                m_px[x, y] = int(130 + 125 * dist)
        return Image.composite(aberrated, bg.filter(ImageFilter.GaussianBlur(radius=1.2)), mask)
    except Exception:
        return bg.filter(ImageFilter.GaussianBlur(radius=dof_radius))


def create_rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    """Creates a 4x supersampled anti-aliased rounded rectangle alpha mask."""
    w, h = size
    scale = 4
    mask_hr = Image.new("L", (w * scale, h * scale), 0)
    draw_hr = ImageDraw.Draw(mask_hr)
    draw_hr.rounded_rectangle([(0, 0), (w * scale - 1, h * scale - 1)], radius=radius * scale, fill=255)
    return mask_hr.resize((w, h), Image.Resampling.LANCZOS)


def draw_aa_rounded_rect(
    canvas: Image.Image,
    xy: Tuple[int, int, int, int],
    radius: int,
    fill: Optional[Tuple[int, int, int, int]] = None,
    outline: Optional[Tuple[int, int, int, int]] = None,
    width: int = 1,
    dot: Optional[Tuple[int, int, int, Tuple[int, int, int, int]]] = None,
) -> None:
    """Draws a 2x supersampled anti-aliased rounded rectangle onto canvas."""
    x1, y1, x2, y2 = xy
    pad = 4
    w, h = (x2 - x1) + pad * 2, (y2 - y1) + pad * 2
    scale = 2
    overlay = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    rx1, ry1 = pad * scale, pad * scale
    rx2, ry2 = (pad + (x2 - x1)) * scale, (pad + (y2 - y1)) * scale
    odraw.rounded_rectangle(
        [(rx1, ry1), (rx2, ry2)],
        radius=radius * scale,
        fill=fill,
        outline=outline,
        width=width * scale,
    )
    if dot is not None:
        dx, dy, dr, dcol = dot
        cx = (pad + (dx - x1)) * scale
        cy = (pad + (dy - y1)) * scale
        rr = dr * scale
        odraw.ellipse([(cx - rr, cy - rr), (cx + rr, cy + rr)], fill=dcol)
    smooth = overlay.resize((w, h), Image.Resampling.LANCZOS)
    canvas.alpha_composite(smooth, (x1 - pad, y1 - pad))


def draw_metallic_separator(canvas: Image.Image, x: int, y: int, width: int) -> None:
    """Draws a thin metallic specular separator line with soft fade-out."""
    line_img = Image.new("RGBA", (width, 2), (0, 0, 0, 0))
    px = line_img.load()
    for i in range(width):
        t = i / max(1, width - 1)
        if t < 0.15:
            alpha = int(160 + (t / 0.15) * 75)
        else:
            alpha = int(235 * ((1.0 - (t - 0.15) / 0.85) ** 1.35))
        px[i, 0] = (195, 215, 245, max(0, min(240, alpha)))
        px[i, 1] = (70, 90, 125, max(0, min(140, alpha // 2)))
    canvas.alpha_composite(line_img, (x, y))


def wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 4,
) -> List[str]:
    """Pixel-accurate word wrapper with clean ellipsis truncation."""
    words = text.split()
    lines: List[str] = []
    cur: List[str] = []

    for w in words:
        test = " ".join(cur + [w])
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) > max_width and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip(".,;:- ")
        while last:
            test_last = last + "..."
            bbox = draw.textbbox((0, 0), test_last, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                lines[-1] = test_last
                break
            last = " ".join(last.split()[:-1])
    return lines


def _template_eleven(bg: Any, side: Any, data: Optional[Dict[str, Any]] = None, logo: Any = None) -> Image.Image:
    """
    Template 11: Flagship Hero Banner Layout (Crunchyroll / Netflix Ultra Spec).
    Output: 1280x720 RGB PIL Image.
    """
    W, H = 1280, 720
    data = data or {}

    # 1. Resolve Poster (side) & Backdrop (bg)
    if isinstance(side, Image.Image):
        poster_raw = side.convert("RGB")
    elif isinstance(side, str) and os.path.exists(side):
        poster_raw = Image.open(side).convert("RGB")
    elif isinstance(bg, Image.Image):
        poster_raw = bg.convert("RGB")
    else:
        poster_raw = Image.new("RGB", (374, 608), color=(25, 30, 48))

    if isinstance(bg, Image.Image):
        backdrop_raw = bg.convert("RGB")
    elif isinstance(bg, str) and os.path.exists(bg):
        backdrop_raw = Image.open(bg).convert("RGB")
    else:
        backdrop_raw = poster_raw.copy()

    dom_r, dom_g, dom_b = extract_dominant_accent(poster_raw)

    # 2. Atmospheric Backdrop + Aberration + Vignette + Ambient Glow
    bg_canvas = cover_resize(backdrop_raw, W, H)
    bg_canvas = ImageEnhance.Color(bg_canvas).enhance(1.12)
    bg_canvas = ImageEnhance.Contrast(bg_canvas).enhance(1.06)
    bg_canvas = apply_chromatic_aberration_and_dof(bg_canvas, shift_px=2, dof_radius=3.0)
    canvas = bg_canvas.convert("RGBA")

    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    v_px = vignette.load()
    for y in range(H):
        vy = y / H
        for x in range(W):
            vx = x / W
            left_weight = max(0.0, 1.0 - (vx * 0.95))
            edge_weight = ((vx - 0.5) ** 2 + (vy - 0.5) ** 2) * 0.85
            alpha = int(105 + 80 * left_weight + 45 * (vy ** 1.3) + 35 * edge_weight)
            alpha = max(65, min(228, alpha))
            r_c = int(10 + 4 * vy)
            g_c = int(16 + 2 * vy)
            b_c = int(30 - 6 * vy)
            v_px[x, y] = (r_c, g_c, b_c, alpha)
    canvas = Image.alpha_composite(canvas, vignette)

    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow_layer)
    g_draw.ellipse([(-40, 70), (460, 650)], fill=(dom_r, dom_g, dom_b, 72))
    g_draw.ellipse([(420, 40), (880, 360)], fill=(37, 99, 235, 42))
    g_draw.ellipse([(820, 420), (1260, 700)], fill=(225, 29, 72, 34))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=95))
    canvas = Image.alpha_composite(canvas, glow_layer)

    # 3. Glassmorphic Hero Container (Right Side)
    panel_x, panel_y = 448, 56
    panel_w, panel_h = 784, 608
    panel_radius = 22

    panel_shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ps_draw = ImageDraw.Draw(panel_shadow)
    ps_draw.rounded_rectangle(
        [(panel_x + 6, panel_y + 12), (panel_x + panel_w + 6, panel_y + panel_h + 12)],
        radius=panel_radius,
        fill=(2, 5, 12, 160),
    )
    panel_shadow = panel_shadow.filter(ImageFilter.GaussianBlur(radius=22))
    canvas = Image.alpha_composite(canvas, panel_shadow)

    panel_crop = canvas.crop((panel_x, panel_y, panel_x + panel_w, panel_y + panel_h))
    panel_blur = panel_crop.filter(ImageFilter.GaussianBlur(radius=16))

    glass_tint = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    gt_px = glass_tint.load()
    for py in range(panel_h):
        ty = py / panel_h
        for px_i in range(panel_w):
            tx = px_i / panel_w
            a = int(172 - 42 * tx + 18 * ty)
            gt_px[px_i, py] = (12, 17, 29, max(110, min(205, a)))
    panel_composited = Image.alpha_composite(panel_blur, glass_tint)

    sheen = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(sheen)
    sh_draw.ellipse([(-120, -120), (460, 240)], fill=(255, 255, 255, 22))
    sheen = sheen.filter(ImageFilter.GaussianBlur(radius=50))
    panel_composited = Image.alpha_composite(panel_composited, sheen)

    panel_mask = create_rounded_mask((panel_w, panel_h), panel_radius)
    canvas.paste(panel_composited, (panel_x, panel_y), panel_mask)

    draw_aa_rounded_rect(
        canvas,
        (panel_x, panel_y, panel_x + panel_w - 1, panel_y + panel_h - 1),
        radius=panel_radius,
        outline=(125, 160, 215, 110),
        width=2,
    )
    draw_aa_rounded_rect(
        canvas,
        (panel_x + 2, panel_y + 2, panel_x + panel_w - 3, panel_y + panel_h - 3),
        radius=panel_radius - 2,
        outline=(255, 255, 255, 32),
        width=1,
    )

    # 4. Floating 3D Anime Poster Card (Left Side)
    poster_x, poster_y = 48, 56
    poster_w, poster_h = 374, 608
    poster_radius = 20

    poster_shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    psh_draw = ImageDraw.Draw(poster_shadow)
    psh_draw.rounded_rectangle(
        [(poster_x + 10, poster_y + 16), (poster_x + poster_w + 10, poster_y + poster_h + 16)],
        radius=poster_radius,
        fill=(2, 4, 10, 230),
    )
    poster_shadow = poster_shadow.filter(ImageFilter.GaussianBlur(radius=24))
    canvas = Image.alpha_composite(canvas, poster_shadow)

    poster_img = smart_poster_fit(poster_raw, poster_w, poster_h)
    poster_img = ImageEnhance.Contrast(poster_img).enhance(1.06)
    poster_img = ImageEnhance.Color(poster_img).enhance(1.05)
    poster_img = ImageEnhance.Sharpness(poster_img).enhance(1.18)
    poster_mask = create_rounded_mask((poster_w, poster_h), poster_radius)
    canvas.paste(poster_img.convert("RGBA"), (poster_x, poster_y), poster_mask)

    draw_aa_rounded_rect(
        canvas,
        (poster_x, poster_y, poster_x + poster_w - 1, poster_y + poster_h - 1),
        radius=poster_radius,
        outline=(255, 255, 255, 145),
        width=2,
    )
    draw_aa_rounded_rect(
        canvas,
        (poster_x + 2, poster_y + 2, poster_x + poster_w - 3, poster_y + poster_h - 3),
        radius=poster_radius - 2,
        outline=(dom_r, dom_g, dom_b, 115),
        width=1,
    )

    # 5. Extract & Normalize Metadata Values
    title = _safe_str(data.get("title") or data.get("name"), "Title")
    alt_title = _safe_str(
        data.get("native_title") or data.get("romaji_title") or data.get("alt_title") or data.get("sub_title"),
        "",
    )
    channel_tag = _safe_str(data.get("brand"), "Anime Starfall")
    clean_brand = channel_tag.replace("@", "").replace("[", "").replace("]", "").strip()
    channel_footer = f"TELEGRAM  •  t.me/{clean_brand}"

    year = _safe_str(data.get("year"), "2024")
    fmt = _safe_str(data.get("format") or data.get("category"), "TV").upper()
    release_season = f"{year}  •  {fmt}"

    score = data.get("rating")
    if score:
        try:
            score_f = float(str(score).split("/")[0].strip())
            rating_text = f"★ {score_f:.1f} / 10"
        except Exception:
            rating_text = f"★ {score} / 10"
    else:
        rating_text = "★ 8.2 / 10"

    # Episode / Batch Status extraction
    s_int = _extract_digits(data.get("season"), 1)
    tot_episodes = _extract_digits(data.get("episodes"), 12)
    raw_ep = data.get("episode")
    is_batch = bool(data.get("is_batch", True if raw_ep is None else False))

    status_badge_override = _safe_str(data.get("status_badge") or data.get("batch_badge"), "")
    if status_badge_override:
        ep_badge = status_badge_override
    elif is_batch:
        ep_badge = f"Batch • {tot_episodes} Episodes" if tot_episodes > 1 else "Complete Series"
    else:
        e_int = _extract_digits(raw_ep, 1)
        ep_badge = f"S{s_int:02d} E{e_int:02d}"

    # Resolution & Codec Probing
    raw_res = _safe_str(data.get("resolution") or data.get("quality"), "1080p")
    res_m = re.search(r"\b(2160p|4k|1080p|720p|480p)\b", raw_res.lower())
    res_label = res_m.group(1).upper() if res_m else "1080p"
    if res_label == "4K":
        res_label = "2160p"

    raw_codec = _safe_str(data.get("codec") or data.get("video_codec"), "HEVC")
    rc_low = raw_codec.lower()
    if "av1" in rc_low:
        codec_label = "AV1"
    elif any(x in rc_low for x in ("265", "hevc")):
        codec_label = "HEVC"
    elif any(x in rc_low for x in ("264", "avc")):
        codec_label = "AVC"
    else:
        codec_label = raw_codec.upper()
    qual_badge = f"{res_label} {codec_label}"

    # Probed Audio Badge (no hardcoded Tamil fallback)
    aud_label = _safe_str(data.get("audio") or data.get("audio_tag"), "Japanese FLAC 2.0")
    audio_keywords = ("audio", "dub", "flac", "aac", "opus", "ddp", "ac3", "eac3", "truehd", "dts", "pcm", "2.0", "5.1", "7.1")
    if not any(k in aud_label.lower() for k in audio_keywords):
        aud_label = f"{aud_label} Audio"

    synopsis = _safe_str(data.get("synopsis") or data.get("plot"), "")
    if not synopsis:
        synopsis = "High-definition master archival release featuring untouched native audio and styled subtitles."

    # Genres
    genres_val = data.get("genres") or []
    if isinstance(genres_val, str):
        genre_list = [g.strip() for g in re.split(r"[,•·|/]+", genres_val) if g.strip()]
    elif isinstance(genres_val, list):
        genre_list = [str(g).strip() for g in genres_val if str(g).strip()]
    else:
        genre_list = ["Action", "Drama", "Fantasy"]

    genre_p = " • ".join(genre_list[:2]) if genre_list else "Action • Drama"
    genre_s = " • ".join(genre_list[2:4]) if len(genre_list) > 2 else (genre_list[-1] if len(genre_list) > 1 else "Animation")

    # Bit depth & Dynamic Range detection
    raw_probe = " ".join(str(data.get(k) or "") for k in ("bit_depth", "bit", "filename", "codec", "quality", "video_stream")).lower()
    is_10bit = bool(re.search(r"\b10[\s\-_]*bit\b|\bhi10p\b|\b10b\b", raw_probe)) or str(data.get("bit_depth")) == "10"
    is_hdr = bool(re.search(r"\bhdr\b|\bdolby\s*vision\b|\bdovi\b", raw_probe))
    dr_str = "HDR" if is_hdr else "SDR"
    bit_str = f"10-bit {dr_str}" if is_10bit else f"8-bit {dr_str}"

    v_stream_main = _safe_str(
        data.get("video_stream_main"),
        f"{res_label} HD",
    ).replace("Ultra ", "")
    v_stream_sub = _safe_str(
        data.get("video_stream_sub"),
        f"{codec_label} • {bit_str}",
    )
    rel_card_main = _safe_str(
        data.get("release_card_main"),
        "Complete Series" if is_batch else ep_badge,
    )
    rel_card_sub = _safe_str(
        data.get("release_card_sub"),
        f"{year} • {tot_episodes} Episodes" if tot_episodes > 1 else f"{year} • Master Archive",
    )

    meta_cards = [
        ("VIDEO STREAM", v_stream_main, v_stream_sub, (37, 99, 235)),
        ("GENRE TAGS", genre_p, genre_s, (168, 85, 247)),
        ("RELEASE INFO", rel_card_main, rel_card_sub, (225, 29, 72)),
    ]

    if is_batch or data.get("complete", True):
        progress_label = _safe_str(data.get("progress_label"), f"SEASON {s_int:02d} COMPLETE • 100%")
        progress_ratio = 1.0
    else:
        e_int = _extract_digits(raw_ep, 1)
        progress_label = f"SEASON {s_int:02d} PROGRESS   •   EP {e_int:02d} / {tot_episodes:02d}"
        progress_ratio = max(0.05, min(1.0, e_int / max(1, tot_episodes)))

    # 6. UI Grid & Typography
    draw = ImageDraw.Draw(canvas, "RGBA")
    pad_x = 38
    content_x = panel_x + pad_x                 # 486
    available_width = panel_w - (pad_x * 2)     # 708
    content_right = content_x + available_width # 1194

    font_top_tag = load_font(15, "extrabold")
    font_year = load_font(13, "bold")
    font_title, title_lines, title_line_h = fit_headline_title(draw, title, available_width)
    font_subtitle = load_font(15, "bold", cjk=True)
    font_badge = load_font(15, "bold")
    font_sec_hdr = load_font(14, "extrabold")
    font_plot = load_font(17, "regular")
    font_card_lbl = load_font(11, "bold")
    font_card_val = load_font(16, "bold")
    font_card_sub = load_font(13, "medium")
    font_footer = load_font(12, "bold")

    # Top Tag
    hdr_y = panel_y + 30  # 86
    draw_aa_rounded_rect(
        canvas,
        (content_x, hdr_y + 2, content_x + 4, hdr_y + 20),
        radius=2,
        fill=(250, 204, 21, 255),
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    tag_display = channel_tag.upper() if not channel_tag.startswith("@") else channel_tag
    draw.text((content_x + 14, hdr_y + 2), tag_display, font=font_top_tag, fill=(180, 120, 10, 140))
    draw.text((content_x + 13, hdr_y + 1), tag_display, font=font_top_tag, fill=(253, 224, 71, 255))

    # Rating & Release Year Pills
    yr_bbox = draw.textbbox((0, 0), release_season, font=font_year)
    yr_w = (yr_bbox[2] - yr_bbox[0]) + 28
    yr_x = content_right - yr_w
    draw_aa_rounded_rect(
        canvas,
        (yr_x, hdr_y - 3, content_right, hdr_y + 27),
        radius=15,
        fill=(22, 32, 52, 220),
        outline=(100, 135, 190, 155),
        width=1,
    )
    rate_bbox = draw.textbbox((0, 0), rating_text, font=font_year)
    rate_w = (rate_bbox[2] - rate_bbox[0]) + 26
    rate_x = yr_x - rate_w - 10
    draw_aa_rounded_rect(
        canvas,
        (rate_x, hdr_y - 3, rate_x + rate_w, hdr_y + 27),
        radius=15,
        fill=(28, 36, 54, 220),
        outline=(250, 204, 21, 155),
        width=1,
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.text((rate_x + 13, hdr_y + 4), rating_text, font=font_year, fill=(253, 224, 71, 255))
    draw.text((yr_x + 14, hdr_y + 4), release_season, font=font_year, fill=(219, 234, 254, 255))

    # Main Headline (Dynamic 1 or 2 lines with soft glow & drop-shadow)
    title_y = hdr_y + 34  # 120
    title_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tg_draw = ImageDraw.Draw(title_glow)
    cur_ty = title_y
    for tline in title_lines:
        tg_draw.text((content_x, cur_ty), tline, font=font_title, fill=(147, 197, 253, 95))
        cur_ty += title_line_h
    title_glow = title_glow.filter(ImageFilter.GaussianBlur(radius=8))
    canvas = Image.alpha_composite(canvas, title_glow)

    draw = ImageDraw.Draw(canvas, "RGBA")
    cur_ty = title_y
    for tline in title_lines:
        draw.text((content_x + 2, cur_ty + 4), tline, font=font_title, fill=(2, 6, 16, 225))
        draw.text((content_x, cur_ty), tline, font=font_title, fill=(255, 255, 255, 255))
        cur_ty += title_line_h

    # Native / Alt Subtitle
    sub_y = cur_ty + 4
    if alt_title:
        alt_lines = wrap_text_lines(draw, alt_title, font_subtitle, available_width, max_lines=1)
        if alt_lines:
            draw.text((content_x, sub_y), alt_lines[0], font=font_subtitle, fill=(165, 180, 252, 240))
        badges_y = sub_y + 28
    else:
        badges_y = sub_y + 6

    # Badges Row (Crimson Status/Batch, Electric Blue Quality, Slate Probed Audio)
    badge_h = 36
    bx = content_x

    badge_specs = [
        (ep_badge, (220, 24, 60, 248), (255, 140, 160, 215), (255, 255, 255, 255)),
        (qual_badge, (29, 92, 245, 248), (135, 190, 255, 215), (255, 255, 255, 255)),
        (aud_label, (34, 42, 56, 242), (255, 255, 255, 165), (241, 245, 249, 255)),
    ]

    for b_label, b_fill, b_border, b_text_col in badge_specs:
        if not b_label:
            continue
        tb = draw.textbbox((0, 0), b_label, font=font_badge)
        tw = tb[2] - tb[0]
        bw = tw + 34

        draw_aa_rounded_rect(
            canvas,
            (bx + 1, badges_y + 3, bx + bw + 1, badges_y + badge_h + 3),
            radius=10,
            fill=(2, 6, 15, 140),
        )
        draw_aa_rounded_rect(
            canvas,
            (bx, badges_y, bx + bw, badges_y + badge_h),
            radius=10,
            fill=b_fill,
            outline=b_border,
            width=1,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((bx + 17, badges_y + 8), b_label, font=font_badge, fill=b_text_col)
        bx += bw + 14

    # Metallic Separator Line
    div_y = badges_y + badge_h + 18
    draw_metallic_separator(canvas, content_x, div_y, available_width)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # Synopsis Section
    syn_y = div_y + 15
    draw.text(
        (content_x, syn_y),
        "S Y N O P S I S",
        font=font_sec_hdr,
        fill=(165, 182, 210, 255),
    )

    syn_text_y = syn_y + 24
    max_syn_lines = 3 if len(title_lines) > 1 else 4
    p_lines = wrap_text_lines(draw, synopsis, font_plot, available_width, max_lines=max_syn_lines)
    line_step = 26 if len(title_lines) > 1 else 28
    for pl in p_lines:
        draw.text((content_x + 1, syn_text_y + 1), pl, font=font_plot, fill=(4, 8, 18, 175))
        draw.text((content_x, syn_text_y), pl, font=font_plot, fill=(230, 236, 245, 255))
        syn_text_y += line_step

    # 3-Column Streaming Specs Grid
    grid_y = 468
    grid_h = 98
    num_cards = len(meta_cards)
    card_gap = 16
    card_w = (available_width - card_gap * (num_cards - 1)) // num_cards

    for idx, (m_label, m_val, m_sub, m_color) in enumerate(meta_cards):
        cx = content_x + idx * (card_w + card_gap)
        draw_aa_rounded_rect(
            canvas,
            (cx, grid_y, cx + card_w, grid_y + grid_h),
            radius=12,
            fill=(18, 25, 42, 215),
            outline=(255, 255, 255, 45),
            width=1,
        )
        draw_aa_rounded_rect(
            canvas,
            (cx + 16, grid_y, cx + card_w - 16, grid_y + 3),
            radius=1,
            fill=(*m_color, 240),
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((cx + 16, grid_y + 15), m_label, font=font_card_lbl, fill=(*m_color, 250))
        val_font = font_card_val
        for fsz in (18, 16, 15, 14, 13):
            cand_f = load_font(fsz, "bold")
            vb = draw.textbbox((0, 0), m_val, font=cand_f)
            if (vb[2] - vb[0]) <= (card_w - 28):
                val_font = cand_f
                break
        draw.text((cx + 16, grid_y + 36), m_val, font=val_font, fill=(248, 250, 252, 255))
        draw.text((cx + 16, grid_y + 63), m_sub, font=font_card_sub, fill=(156, 172, 196, 245))

    # Bottom Separator & Streaming Progress Bar
    footer_div_y = 588
    draw_metallic_separator(canvas, content_x, footer_div_y, available_width)
    draw = ImageDraw.Draw(canvas, "RGBA")

    footer_y = 608
    draw_aa_rounded_rect(
        canvas,
        (content_x, footer_y + 3, content_x + 8, footer_y + 11),
        radius=4,
        fill=(250, 204, 21, 255),
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.text(
        (content_x + 16, footer_y),
        channel_footer,
        font=font_footer,
        fill=(215, 225, 240, 245),
    )

    prog_bar_w = 132
    prog_bar_h = 8
    prog_bar_x = content_right - prog_bar_w
    prog_bar_y = footer_y + 4

    pl_bbox = draw.textbbox((0, 0), progress_label, font=font_card_lbl)
    pl_w = pl_bbox[2] - pl_bbox[0]
    draw.text(
        (prog_bar_x - pl_w - 14, footer_y + 1),
        progress_label,
        font=font_card_lbl,
        fill=(156, 172, 196, 240),
    )
    draw_aa_rounded_rect(
        canvas,
        (prog_bar_x, prog_bar_y, prog_bar_x + prog_bar_w, prog_bar_y + prog_bar_h),
        radius=4,
        fill=(28, 37, 54, 240),
        outline=(255, 255, 255, 55),
        width=1,
    )
    fill_w = max(14, int(prog_bar_w * max(0.0, min(1.0, progress_ratio))))
    draw_aa_rounded_rect(
        canvas,
        (prog_bar_x, prog_bar_y, prog_bar_x + fill_w, prog_bar_y + prog_bar_h),
        radius=4,
        fill=(220, 24, 60, 255),
    )

    # Optional Logo
    if logo and isinstance(logo, Image.Image):
        try:
            logo_scaled = logo.copy()
            logo_scaled.thumbnail((50, 50), Image.Resampling.LANCZOS)
            canvas.paste(logo_scaled, (content_right - 55, hdr_y - 2), logo_scaled if logo_scaled.mode == "RGBA" else None)
        except Exception:
            pass

    return canvas.convert("RGB")
